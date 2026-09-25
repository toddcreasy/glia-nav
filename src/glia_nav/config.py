from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Locally from .env, deployed from App Runner env vars.

    Nothing here is a secret. The database password stays in Secrets Manager and is
    resolved by the RDS Data API from its ARN; this process never reads its value.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str = "us-east-1"
    database_cluster_arn: str
    database_secret_arn: str
    database_name: str = "glianav"
    documents_bucket: str = ""
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    log_level: str = "INFO"
    cognito_user_pool_id: str
    cognito_client_id: str
    # Tokens the public demo's chat may bill per day, US Eastern, before it refuses.
    daily_token_limit: int = 150_000
    # Where AgentStack publishes the runtime ARN; BackendStack cannot reference it.
    agent_runtime_parameter: str = "/glia-nav/agent-runtime-arn"
    # Where FrontendStack publishes the Amplify origin, the one origin CORS allows.
    frontend_origin_parameter: str = "/glia-nav/frontend-origin"


@lru_cache
def get_settings() -> Settings:
    return Settings()


class AgentSettings(BaseSettings):
    """Configuration for the agent process, which runs on AgentCore Runtime.

    Separate from Settings: the agent container has no database access and so is
    never given the Data API ARNs that Settings requires.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str = "us-east-1"
    model_small: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    model_large: str = "us.anthropic.claude-opus-4-6-v1"
    # Opus writes longer answers than Haiku did; at 1024 it stopped mid-answer on a third
    # of the golden set's search cases.
    max_output_tokens: int = 4096
    gateway_url: str
    guardrail_id: str = ""
    guardrail_version: str = ""
    agent_runtime_arn: str = ""
    log_level: str = "INFO"


@lru_cache
def get_agent_settings() -> AgentSettings:
    return AgentSettings()


class IngestSettings(BaseSettings):
    """Configuration for ingestion: the backfill script locally, the scheduled job deployed.

    Separate from Settings because ingestion has no use for the Cognito config.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    aws_region: str = "us-east-1"
    database_cluster_arn: str
    database_secret_arn: str
    database_name: str = "glianav"
    documents_bucket: str
    embedding_model: str = "amazon.titan-embed-text-v2:0"
    # Reads structured eligibility out of each trial's criteria text.
    model_small: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    ncbi_api_key: SecretStr | None = None
    log_level: str = "INFO"
