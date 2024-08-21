import logging
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

import numpy as np
import pandas as pd
from elasticsearch import Elasticsearch

from .monitoring_stats_connector import (ClusterStats, IndexStats, NodeStats,
                                         ShardStats, elasticsearch_id_filter,
                                         range_filter)

logger = logging.getLogger(__name__)


# Helper function to compute the percentage of the total for a given column
# compared to the entire group, per the "group" list.
def _compute_pct(datastreams: pd.DataFrame, column: str, group: List[str]) -> pd.Series:
    """
    Compute the percentage of the total for a given column.
    """
    # Catch division by zero
    return datastreams[column].astype(float) / (
        datastreams.groupby(group)[column]
        .transform("sum")
        .astype(float)
        .replace(0, np.nan)
    )


class DeploymentDataProcessor:
    # TODO: link this to the Stats class
    chunk_size = "10min"
    chunk_size_seconds = pd.to_timedelta(chunk_size).total_seconds()
    hour_ratio = chunk_size_seconds / 3600

    def __init__(
        self,
        es: Elasticsearch,
        elasticsearch_id: str,
        from_ts: datetime,
        price_df: pd.DataFrame,
        monitoring_index_pattern: Optional[str] = None,
        parsing_regex_str: Optional[str] = None,
    ):
        self.es = es
        self.elasticsearch_id = elasticsearch_id
        self.from_ts = from_ts
        self.to_ts = from_ts + timedelta(hours=1)

        self.cost_data = price_df
        self.skip_prices = self.cost_data.empty
        if self.skip_prices:
            logger.warning(
                f"No cost data found for {self.elasticsearch_id} "
                f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}, "
                f"cost computation will be skipped"
            )

        # TODO: search index_data, node_data and shard_data in parallel
        self.index_data = IndexStats(
            es, monitoring_index_pattern, parsing_regex_str
        ).search_as_dataframe(
            [
                # Shift 1 chunk forward to avoid missing data
                range_filter(
                    self.from_ts - timedelta(seconds=self.chunk_size_seconds),
                    self.to_ts,
                ),
                elasticsearch_id_filter(self.elasticsearch_id),
            ],
        )

        # If there's no data, we stop here
        if self.index_data.empty:
            logger.error(
                f"No index data found for {self.elasticsearch_id} "
                f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}, "
                f"skipping further processing"
            )
            return

        # TODO: run in parallel
        self.cluster_data = ClusterStats(
            es, monitoring_index_pattern
        ).search_as_dataframe(
            [
                # Shift 1 chunk forward to avoid missing data
                range_filter(
                    self.from_ts - timedelta(seconds=self.chunk_size_seconds),
                    self.to_ts,
                ),
                elasticsearch_id_filter(self.elasticsearch_id),
            ],
        )

        # TODO: search index_data, node_data and shard_data in parallel
        self.node_data = (
            NodeStats(es, monitoring_index_pattern)
            .search_as_dataframe(
                [
                    # Shift 1 chunk forward to avoid missing data
                    range_filter(
                        self.from_ts - timedelta(seconds=self.chunk_size_seconds),
                        self.to_ts,
                    ),
                    elasticsearch_id_filter(self.elasticsearch_id),
                ],
            )
            .merge(
                self.cluster_data,
                left_on=["@timestamp", "id"],
                right_on=["@timestamp", "id"],
                how="left",
                suffixes=(None, ""),
            )
            .dropna(subset=["tier"])  # No tier information => not a data node (we drop)
        )

        if not self.skip_prices:
            self.node_data = self.node_data.join(self.cost_data, on="tier")

            # Compute the actual price of the node for the corresponding time chunk
            self.node_data["cost"] = (
                self.node_data["price_per_hour_per_gb"]
                * (self.node_data["memory_limit_bytes"] / 1024 / 1024 / 1024)
                * self.hour_ratio
            )

            # Compute the cost of each tier
            self.node_data["tier_cost"] = self.node_data.groupby(
                ["tier", "@timestamp"]
            )["cost"].transform("sum")
        else:
            self.node_data["tier_cost"] = None

        # TODO: perform data fetches in parallel
        shard_data = (
            ShardStats(es, monitoring_index_pattern).search_as_dataframe(
                [
                    # For shard data we need to go back even further to ensure
                    # we can properly identify the node where the data lives.
                    range_filter(
                        self.from_ts - timedelta(seconds=5 * self.chunk_size_seconds),
                        self.to_ts,
                    ),
                    elasticsearch_id_filter(self.elasticsearch_id),
                ],
            )
            # Join with node_data to add the tier information
            .join(
                self.node_data[
                    [
                        "id",
                        "tier",
                        "deployment_name",
                        "elasticsearch_id",
                        "tier_cost",
                    ]
                ]
                .drop_duplicates()
                .set_index("id"),
                on="node_uuid",
            )
        )

        logger.debug(
            f"Enriching index data with tier information for {self.elasticsearch_id} "
            f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        self.index_data = self.index_data.merge(
            shard_data[
                [
                    "@timestamp",
                    "index_name",
                    "tier",
                    "tier_cost",
                    "deployment_name",
                    "elasticsearch_id",
                ]
            ],
            left_on=["@timestamp", "name"],
            right_on=["@timestamp", "index_name"],
            how="left",
            suffixes=(None, ""),
        )

    def _get_datastreams_usages(self) -> Iterable:
        if self.index_data.empty:
            logger.warning("Empty dataset, skipping datastreams usages")
            return ()

        logger.debug(
            f"Grouping {len(self.index_data)} indices "
            f"in datastream usages for {self.elasticsearch_id} "
            f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        datastream_usages = self.index_data.groupby(
            ["datastream", "tier", "age_days", "@timestamp"]
        ).agg(
            {
                "deployment_name": "first",
                "elasticsearch_id": "first",
                "tier_cost": "first",
                "primary_docs_count": "sum",
                "primary_store_size_in_bytes": "sum",
                "total_docs_count": "sum",
                "total_store_size_in_bytes": "sum",
                "search_query_total_delta": "sum",
                "search_query_time_in_seconds_delta": "sum",
            }
        )

        logger.debug(
            f"Computing datastream usages percentages for {self.elasticsearch_id} "
            f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        group = ["tier", "@timestamp"]

        # Compute the percentage of the total for each column
        datastream_usages["search_query_time_in_seconds_pct"] = _compute_pct(
            datastream_usages, "search_query_time_in_seconds_delta", group
        )
        datastream_usages["total_store_size_in_bytes_pct"] = _compute_pct(
            datastream_usages, "total_store_size_in_bytes", group
        )

        if not self.skip_prices:
            logger.debug(
                f"Computing datastream usages costs for {self.elasticsearch_id} "
                f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # Compute the cost for each datastream/tier for the chunk
            datastream_usages["search_cost"] = (
                datastream_usages["search_query_time_in_seconds_pct"]
                * datastream_usages["tier_cost"]
            )
            datastream_usages["storage_cost"] = (
                datastream_usages["total_store_size_in_bytes_pct"]
                * datastream_usages["tier_cost"]
            )

        datastream_usages = datastream_usages.drop(
            columns=[
                "tier_cost",
            ],
        )

        datastream_usages = datastream_usages.reset_index().rename(
            columns={"@timestamp": "timestamp"}
        )

        # Put None instead of NaN
        datastream_usages = datastream_usages.apply(np.nan_to_num).fillna(0)

        logger.debug(
            f"{len(datastream_usages)} datastream usages found for "
            f"{self.elasticsearch_id} at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        datastream_usages["dataset"] = "datastream_usage"

        return list(datastream_usages.reset_index().itertuples(index=False))

    def _get_datastreams(self) -> Iterable:
        if self.index_data.empty:
            logger.warning("Empty dataset, skipping datastreams")
            return ()

        logger.debug(
            f"Grouping {len(self.index_data)} indices "
            f"in datastreams for {self.elasticsearch_id} "
            f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        # Compute stats per datastream
        datastreams = self.index_data.groupby(["datastream", "tier", "@timestamp"]).agg(
            {
                "deployment_name": "first",
                "elasticsearch_id": "first",
                "tier_cost": "first",
                "primary_docs_count": "sum",
                "primary_store_size_in_bytes": "sum",
                "total_docs_count": "sum",
                "total_store_size_in_bytes": "sum",
                "total_store_size_in_bytes_delta": "sum",
                "search_query_total_delta": "sum",
                "search_query_time_in_seconds_delta": "sum",
                "index_total_delta": "sum",
                "index_time_in_seconds_delta": "sum",
                "primary_docs_count_delta": "sum",
                "primary_store_size_in_bytes_delta": "sum",
            }
        )

        logger.debug(
            f"Computing datastreams percentages for {self.elasticsearch_id} "
            f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        group = ["tier", "@timestamp"]

        # Compute the percentage of the total for each column
        datastreams["search_query_time_in_seconds_pct"] = _compute_pct(
            datastreams, "search_query_time_in_seconds_delta", group
        )
        datastreams["index_time_in_seconds_pct"] = _compute_pct(
            datastreams, "index_time_in_seconds_delta", group
        )
        datastreams["total_store_size_in_bytes_pct"] = _compute_pct(
            datastreams, "total_store_size_in_bytes", group
        )

        if not self.skip_prices:
            logger.debug(
                f"Computing datastreams costs for {self.elasticsearch_id} "
                f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
            )

            # Compute the cost for each datastream/tier for the chunk
            datastreams["search_cost"] = (
                datastreams["search_query_time_in_seconds_pct"]
                * datastreams["tier_cost"]
            )
            datastreams["indexing_cost"] = (
                datastreams["index_time_in_seconds_pct"] * datastreams["tier_cost"]
            )
            datastreams["storage_cost"] = (
                datastreams["total_store_size_in_bytes_pct"] * datastreams["tier_cost"]
            )

        datastreams = datastreams.drop(
            columns=[
                "tier_cost",
            ],
        )

        datastreams = datastreams.reset_index().rename(
            columns={"@timestamp": "timestamp"}
        )

        # Put None instead of NaN
        datastreams = datastreams.apply(np.nan_to_num).fillna(0)

        logger.debug(
            f"{len(datastreams)} datastreams found for "
            f"{self.elasticsearch_id} at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        datastreams["dataset"] = "datastream"

        return list(datastreams.reset_index().itertuples(index=False))

    def _get_nodes(self):
        logger.debug(
            f"Computing nodes for {self.elasticsearch_id} "
            f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        if self.index_data.empty:
            logger.warning("Empty dataset, skipping nodes")
            return ()

        nodes = self.node_data.reset_index().rename(columns={"@timestamp": "timestamp"})
        nodes = nodes.apply(np.nan_to_num).fillna(0)

        logger.debug(
            f"{len(nodes)} nodes found for {self.elasticsearch_id} "
            f"at {self.from_ts.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        nodes["dataset"] = "node"

        return list(nodes.reset_index().itertuples(index=False))

    def process(self, compute_usages: bool = False):
        res = self._get_datastreams() + self._get_nodes()
        if compute_usages:
            res += self._get_datastreams_usages()

        return res
