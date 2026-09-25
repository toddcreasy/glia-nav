"""Round-trip check: insert two vectors, score them against a query by cosine similarity."""

import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
CLUSTER_ARN = os.environ["DATABASE_CLUSTER_ARN"]
SECRET_ARN = os.environ["DATABASE_SECRET_ARN"]
DB_NAME = os.environ.get("DATABASE_NAME", "glianav")

rds = boto3.client("rds-data", region_name=REGION)


def sql(statement: str) -> list:
    response = rds.execute_statement(
        resourceArn=CLUSTER_ARN,
        secretArn=SECRET_ARN,
        database=DB_NAME,
        sql=statement,
    )
    return response.get("records", [])


print(f"cluster  {CLUSTER_ARN}")
print(f"database {DB_NAME}\n")

print("version   ", sql("SELECT version()")[0][0]["stringValue"].split(",")[0])
pgvector = sql("SELECT extversion FROM pg_extension WHERE extname='vector'")
print("pgvector  ", pgvector[0][0]["stringValue"])
print("migration ", sql("SELECT version_num FROM alembic_version")[0][0]["stringValue"])
print(
    "tables    ",
    [
        r[0]["stringValue"]
        for r in sql("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    ],
)

sql("DROP TABLE IF EXISTS db_check_vectors")
sql("CREATE TABLE db_check_vectors (id int PRIMARY KEY, label text, embedding vector(3))")
sql("""
    INSERT INTO db_check_vectors (id, label, embedding) VALUES
      (1, 'near', '[1,0,0]'),
      (2, 'far',  '[0,1,0]')
""")

print("\ncosine similarity against query [1,0,0]:")
for record in sql("""
    SELECT label, 1 - (embedding <=> '[1,0,0]') AS similarity
    FROM db_check_vectors
    ORDER BY embedding <=> '[1,0,0]'
"""):
    print(f"  {record[0]['stringValue']:<6} {record[1]['doubleValue']:.4f}")

sql("DROP TABLE db_check_vectors")
print("\nround trip ok")
