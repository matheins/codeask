from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    anthropic_api_key: str
    github_repo_url: str
    github_token: str | None = None
    repo_branch: str = "main"
    clone_dir: str = "./repo"
    model: str = "claude-sonnet-4-5-20250929"
    slack_bot_token: str | None = None
    slack_app_token: str | None = None
    sync_interval: int = 300
    api_key: str | None = None

    model_config = {"env_file": ".env"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
