"""Config the API needs at import time.

Settings requires these, and `.env` is gitignored, so without this the suite only
passes on a machine that happens to have one.
"""

import os

os.environ.setdefault("DATABASE_CLUSTER_ARN", "arn:aws:rds:us-east-1:000000000000:cluster:test")
os.environ.setdefault(
    "DATABASE_SECRET_ARN", "arn:aws:secretsmanager:us-east-1:000000000000:secret:test"
)
os.environ.setdefault("COGNITO_USER_POOL_ID", "us-east-1_test")
os.environ.setdefault("COGNITO_CLIENT_ID", "testclientid")
