import os

import sqlalchemy_aurora_data_api
from alembic import context
from sqlalchemy import create_engine

sqlalchemy_aurora_data_api.register_dialects()

config = context.config
target_metadata = None

CLUSTER_ARN = os.environ["DATABASE_CLUSTER_ARN"]
SECRET_ARN = os.environ["DATABASE_SECRET_ARN"]
DB_NAME = os.environ.get("DATABASE_NAME", "glianav")


def run_migrations_online() -> None:
    engine = create_engine(
        f"postgresql+auroradataapi://:@/{DB_NAME}",
        connect_args={"aurora_cluster_arn": CLUSTER_ARN, "secret_arn": SECRET_ARN},
    )
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
