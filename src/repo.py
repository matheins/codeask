import logging
import threading
import time

import git
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from src.config import get_settings

log = logging.getLogger(__name__)


def _authenticated_url(url: str, token: str) -> str:
    """Inject a token into an HTTPS git URL for private repo access."""
    parsed = urlparse(url)
    authed = parsed._replace(netloc=f"x-access-token:{token}@{parsed.hostname}")
    return urlunparse(authed)


def clone_or_pull() -> str:
    settings = get_settings()
    clone_dir = Path(settings.clone_dir)
    repo_url = settings.github_repo_url

    if settings.github_token and repo_url.startswith("https://"):
        repo_url = _authenticated_url(repo_url, settings.github_token)

    if (clone_dir / ".git").is_dir():
        repo = git.Repo(clone_dir)
        repo.remotes.origin.set_url(repo_url)
        repo.remotes.origin.pull(settings.repo_branch)
        return f"Pulled latest changes on {settings.repo_branch}"

    clone_dir.mkdir(parents=True, exist_ok=True)
    git.Repo.clone_from(
        repo_url,
        clone_dir,
        branch=settings.repo_branch,
    )
    return f"Cloned {settings.github_repo_url} ({settings.repo_branch})"


def start_periodic_sync() -> threading.Thread:
    """Run clone_or_pull() in a background daemon thread on a timer."""
    settings = get_settings()
    interval = settings.sync_interval

    def _loop():
        while True:
            time.sleep(interval)
            try:
                result = clone_or_pull()
                log.info("[sync] %s", result)
            except Exception:
                log.exception("[sync] Failed to sync repo")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info("[sync] Periodic repo sync every %ds", interval)
    return t
