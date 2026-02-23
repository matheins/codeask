from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    github_repo_url: str
    github_token: str | None = None
    repo_branch: str = "main"
    clone_dir: str = "./repo"
    model: str = "claude-sonnet-4-6"
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    sync_interval: int = 300
    api_key: str | None = None
    mcp_servers_config: str | None = None
    max_iterations: int = 20
    max_concurrency: int = 2
    conversation_ttl: int = 3600
    max_history_messages: int = 20
    enable_thinking: bool = True
    thinking_budget: int = 10000
    response_cache_ttl: int = 86400  # 24 hours
    database_url: str | None = None
    db_max_rows: int = 100
    db_query_timeout: int = 30
    custom_instructions: str | None = None

    model_config = {"env_file": ".env"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
