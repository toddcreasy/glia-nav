"""Enable the pgvector extension. One-off, safe to re-run."""

import os

import boto3

rds = boto3.client("rds-data", region_name=os.environ.get("AWS_REGION", "us-east-1"))

rds.execute_statement(
    resourceArn=os.environ["DATABASE_CLUSTER_ARN"],
    secretArn=os.environ["DATABASE_SECRET_ARN"],
    database=os.environ.get("DATABASE_NAME", "glianav"),
    sql="CREATE EXTENSION IF NOT EXISTS vector",
)

result = rds.execute_statement(
    resourceArn=os.environ["DATABASE_CLUSTER_ARN"],
    secretArn=os.environ["DATABASE_SECRET_ARN"],
    database=os.environ.get("DATABASE_NAME", "glianav"),
    sql="SELECT extversion FROM pg_extension WHERE extname = 'vector'",
)
print(f"pgvector {result['records'][0][0]['stringValue']}")
