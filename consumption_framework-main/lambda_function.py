import json
import logging
import os
import sys
from datetime import datetime, timedelta
import boto3
from botocore.exceptions import ClientError

import pytz
import requests

from consumption import Consumption

logger = logging.getLogger(__name__)

def init_logging():
    """Initialize logging configuration."""
    logging.basicConfig(
        stream=sys.stdout,
        format="%(asctime)s [%(threadName)s] - %(levelname)s (%(name)s): %(message)s",
        level=logging.DEBUG if os.environ.get("debug") else logging.INFO,
    )

def _unpack_flat_dict(d):
    """
    Transforms a flat dictionary into a nested dictionary.
    {
        "a.b": 1,
        "a.c": 2,
        "a.d.foo": 3
    }

    into

    {
        "a": {
            "b": 1,
            "c": 2,
            "d": {
                "foo": 3
            }
        }
    }
    """

    result = {}
    for key, value in d.items():
        keys = key.split(".")
        current = result
        for k in keys[:-1]:
            current = current.setdefault(k, {})
        current[keys[-1]] = value
    return result

def check_env_vars(required_vars):
    """Check required environment variables and raise an exception if any are missing."""
    missing_vars = [var for var in required_vars if var not in os.environ]
    if missing_vars:
        missing = ", ".join(missing_vars)
        logger.error(f"Missing required environment variables: {missing}")
        raise EnvironmentError(f"Missing required environment variables: {missing}")

def parse_config(secret_value):
    """Parse and convert config values from a flat dictionary."""
    config = _unpack_flat_dict(secret_value)
    
    if config.get('monitoring_source'):
        # Convert retry_on_timeout to boolean
        retry_on_timeout = config['monitoring_source'].get('retry_on_timeout')
        config['monitoring_source']['retry_on_timeout'] = retry_on_timeout.lower() == 'true' if retry_on_timeout else False
        # Convert request_timeout to integer
        request_timeout = config['monitoring_source'].get('request_timeout')
        config['monitoring_source']['request_timeout'] = int(request_timeout) if request_timeout else None

    return config

def handler(event, context):
    init_logging()

    try:
        # Check for required environment variables
        required_env_vars = ["config_arn", "command", "region"]
        check_env_vars(required_env_vars)

        # Fetch and parse secret configuration
        env = os.environ
        print("Retrieving secret value...")
        secret_value = get_secret(env["config_arn"], env["region"])
        print("Secret value retrieved successfully.")

        config = parse_config(secret_value)

        # Set parameters
        command = env["command"]
        threads = int(env.get("threads", 5))
        lookbehind = int(env.get("lookbehind", 24))
        force = env.get("force", "false").lower() == "true"
        compute_usages = env.get("compute_usages", "false").lower() == "true"
        
        # Define time range for data
        to_ts = datetime.now(tz=pytz.UTC)
        from_ts = to_ts - timedelta(hours=lookbehind)
        print(f"Time range set from {from_ts} to {to_ts}")

        # Base configuration for Consumption
        consumption_base_config = {
            "organization_id": config.get("organization_id"),
            "organization_name": config["organization_name"],
            "billing_api_key": config["billing_api_key"],
            "destination_config": config["consumption_destination"],
            "api_host": "api.elastic-cloud.com",
            "threads": threads,
            "force": force,
            "monitoring_index_pattern": config.get("monitoring_index_pattern", ".monitoring-es-8-*"),
        }

        # Initialize Consumption with monitoring index if needed
        if command == "consume-monitoring":
            consumption = Consumption(
                **consumption_base_config,
                source_config=config["monitoring_source"],
                compute_usages=compute_usages
            )
            consumption.consume_monitoring(from_ts, to_ts)
            print("consume-monitoring completed.")

        elif command == "get-billing-data":
            consumption = Consumption(**consumption_base_config)
            consumption.get_billing_data(from_ts, to_ts)
            print("get-billing-data completed.")

        elif command == "both":
            # Run both consume_monitoring and get_billing_data
            monitoring_consumption = Consumption(
                **consumption_base_config,
                source_config=config["monitoring_source"],
                compute_usages=compute_usages
            )
            monitoring_consumption.consume_monitoring(from_ts, to_ts)
            print("consume-monitoring completed.")

            billing_consumption = Consumption(**consumption_base_config)
            billing_consumption.get_billing_data(from_ts, to_ts)
            print("get-billing-data completed.")

        else:
            logger.error(f"Unknown command: {command}")
            raise ValueError(f"Unknown command: {command}")

        print("Handler execution completed successfully.")
        return "Execution completed successfully."

    except ClientError as e:
        logger.error(f"AWS client error: {e}")
        return {"error": str(e)}
    except EnvironmentError as e:
        logger.error(f"Environment error: {e}")
        return {"error": str(e)}
    except Exception as e:
        logger.exception("An unexpected error occurred.")
        return {"error": str(e)}

def get_secret(secret_name, region_name, session=None):
    """
    Retrieve a secret from AWS Secrets Manager.
    
    Args:
        secret_name (str): The name of the secret to retrieve.
        region_name (str): The AWS region where the secret is stored.
        session (boto3.Session, optional): An existing boto3 session. If not provided, a new session is created.
    
    Returns:
        dict or str: The secret value, parsed as JSON if possible.
        
    Raises:
        ClientError: If any error occurs when retrieving the secret.
    """
    # Use the provided session or create a new one if none is provided
    session = session or boto3.session.Session()
    client = session.client(service_name='secretsmanager', region_name=region_name)

    try:
        # Attempt to retrieve the secret
        get_secret_value_response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        error_code = e.response['Error']['Code']
        logger.error(f"Failed to retrieve secret {secret_name}: {error_code}")
        raise e  # Re-raise the exception for further handling by the caller

    # Check if the secret is a string or binary and decode appropriately
    secret = get_secret_value_response.get('SecretString') or get_secret_value_response.get('SecretBinary')
    
    # If it's binary, decode it
    if isinstance(secret, bytes):
        secret = secret.decode('utf-8')
    
    # Attempt to parse the secret as JSON, fallback to raw string if it fails
    try:
        return json.loads(secret)
    except json.JSONDecodeError:
        logger.warning("Secret is not JSON formatted, returning as raw string.")
        return secret
