import logging
from collections import namedtuple
from datetime import datetime, timedelta
from typing import Dict, Iterable, Optional, Tuple

import pandas as pd
import pytz
from elasticsearch import Elasticsearch
from elasticsearch.helpers import streaming_bulk

from ..utils import (DepDataChecker, ESSBillingClient, ESSResource,
                     MultithreadingEngine)
from .on_prem_costs import get_on_prem_costs
from .processor import DeploymentDataProcessor

logger = logging.getLogger(__name__)

INDEX = "consumption"


class ESSBillingClientCostsProvider(ESSBillingClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tier_prices = {}

    def get_elasticsearch_costs(
        self, deployment_id: str, from_ts: datetime
    ) -> pd.DataFrame:
        # Get the billing data for the given deployment_id
        billing_data = next(
            (
                d
                for d in self.get_billing_data(from_ts)["instances"]
                if d["id"] == deployment_id
            ),
            None,
        )

        if not billing_data:
            logger.error(
                f"No billing data found for {deployment_id} for {from_ts.isoformat()}"
            )

            # An empty dataframe ensures we can still compute some info
            return pd.DataFrame()

        # TODO: merge the below with organization processing
        for line_item in billing_data["product_line_items"]:
            if (
                line_item["name"] != "unknown"
                and line_item["sku"]
                and line_item["kind"] == "elasticsearch"
            ):
                resource = ESSResource(line_item)
                self.tier_prices[resource.tier] = resource._price_per_hour_per_gb

        # TODO: reuse the on_prem_costs function
        df = pd.DataFrame.from_dict(
            self.tier_prices, orient="index", columns=["price_per_hour_per_gb"]
        )
        df.index.name = "tier"

        return df


def _source_walk(
    source_es: Elasticsearch,
    index: str,
    range_start: datetime,
    range_end: datetime,
    size: int = 100,
) -> Iterable[Tuple[datetime, str]]:
    """
    Composite aggregation to walk through the monitoring source data.
    We yield the elasticsearch_id and start of the hour for each block
    where we have monitoring data.
    """
    after = (1e15, "")
    while True:
        res = source_es.search(
            index=index,
            size=0,
            query={
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": range_start.isoformat(),
                                    "lt": min(
                                        range_end,
                                        datetime.now(pytz.utc).replace(
                                            minute=0, second=0, microsecond=0
                                        ),
                                    ).isoformat(),
                                }
                            }
                        },
                        {
                            "term": {
                                "event.dataset": {
                                    "value": "elasticsearch.cluster.stats"
                                }
                            }
                        },
                    ]
                }
            },
            aggs={
                "composite": {
                    "composite": {
                        "after": {
                            "per_hour": after[0],
                            "per_elasticsearch_id": after[1],
                        },
                        "size": size,
                        "sources": [
                            {
                                "per_hour": {
                                    "date_histogram": {
                                        "field": "@timestamp",
                                        "fixed_interval": "1h",
                                        "order": "desc",
                                    }
                                }
                            },
                            {
                                "per_elasticsearch_id": {
                                    "terms": {"field": "elasticsearch.cluster.name"}
                                }
                            },
                        ],
                    }
                }
            },
            filter_path=["aggregations"],
        )["aggregations"]["composite"]

        if not res["buckets"]:
            # We've gone through all the data in scope
            break

        # Used for our next iteration
        after = tuple(res["after_key"].values())

        yield from (
            (
                datetime.fromtimestamp(b["key"]["per_hour"] / 1000, pytz.utc),
                b["key"]["per_elasticsearch_id"],
            )
            for b in res["buckets"]
        )


def _analyze_chunk(
    source_es: Elasticsearch,
    destination_es: Elasticsearch,
    organization_id: Optional[str],
    organization_name: str,
    elasticsearch_id: str,
    from_ts: datetime,
    price_df: pd.DataFrame,
    monitoring_index_pattern: Optional[str] = None,
    parsing_regex_str: Optional[str] = None,
    compute_usages: bool = False,
):
    def _as_elasticsearch_doc(tuple: namedtuple) -> dict:
        source = tuple._asdict()
        source["@timestamp"] = source.pop("timestamp").isoformat()
        del source["index"]

        source["organization_id"] = organization_id
        source["organization_name"] = organization_name

        # Necessary for compatibility with v1
        source["deployment_id"] = source["elasticsearch_id"]

        # Some parameters are shared between node and datastream, and we use them
        # as base for the resulting document ID.
        doc_id = (
            source["dataset"]
            + "|"
            # On-prem can have empty organization_id
            + (source["organization_id"] or "")
            + "|"
            + source["deployment_id"]
            + "|"
            + source["@timestamp"]
            + "|"
            + source["tier"]
            + "|"
        )

        # The id generation process is different for Node and Index
        if source["dataset"] == "node":
            doc_id += source["id"]
        elif source["dataset"] == "datastream":
            doc_id += source["datastream"]
        elif source["dataset"] == "datastream_usage":
            doc_id += source["datastream"] + "|" + str(source["age_days"])
        else:
            raise ValueError(f"Unknown dataset: {source['dataset']}")

        return {
            "_id": doc_id,
            "_index": "consumption",
            "_source": source,
            "_op_type": "index",
        }

    ok_count = 0

    for ok, action in streaming_bulk(
        client=destination_es,
        raise_on_error=False,
        actions=(
            _as_elasticsearch_doc(entry)
            for entry in DeploymentDataProcessor(
                es=source_es,
                elasticsearch_id=elasticsearch_id,
                from_ts=from_ts,
                price_df=price_df,
                monitoring_index_pattern=monitoring_index_pattern,
                parsing_regex_str=parsing_regex_str,
            ).process(compute_usages=compute_usages)
        ),
    ):
        if ok:
            ok_count += 1
        else:
            logger.error(f"Failed to index document: {action}")

    logger.debug(f"Data upload completed: {ok_count} OK")


def monitoring_analyzer(
    source_es: Elasticsearch,
    destination_es: Elasticsearch,
    organization_id: Optional[str],
    organization_name: str,
    billing_api_key: Optional[str],
    range_start: datetime,
    range_end: datetime,
    threads: int,
    force: bool,
    compute_usages: bool,
    api_host: str,
    monitoring_index_pattern: Optional[str] = None,
    parsing_regex_str: Optional[str] = None,
    on_prem_costs_dict: Optional[Dict[str, float]] = None,
    requests_ssl_validation: Optional[bool] = None,
):
    cost_provider = (
        ESSBillingClientCostsProvider(
            api_host, billing_api_key, organization_id, requests_ssl_validation
        )
        if not on_prem_costs_dict
        else None
    )

    checker = DepDataChecker(destination_es, organization_id, force=force)

    with MultithreadingEngine(workers=threads) as engine:
        [
            engine.submit_task(_analyze_chunk, params)
            for params in (
                {
                    "source_es": source_es,
                    "destination_es": destination_es,
                    "organization_id": organization_id,
                    "organization_name": organization_name,
                    "elasticsearch_id": elasticsearch_id,
                    "from_ts": from_ts,
                    "price_df": (
                        get_on_prem_costs(on_prem_costs_dict)
                        if on_prem_costs_dict
                        else cost_provider.get_elasticsearch_costs(
                            elasticsearch_id, from_ts
                        )
                    ),
                    "monitoring_index_pattern": monitoring_index_pattern,
                    "parsing_regex_str": parsing_regex_str,
                    "compute_usages": compute_usages,
                }
                for from_ts, elasticsearch_id in _source_walk(
                    source_es, monitoring_index_pattern, range_start, range_end
                )
                if not checker.is_in_cluster(
                    from_ts,
                    from_ts + timedelta(hours=1),
                    filters=[{"term": {"elasticsearch_id": elasticsearch_id}}],
                )
            )
        ]


__all__ = ["monitoring_analyzer"]
