from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING

import git
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from src.config import get_settings

if TYPE_CHECKING:
    from src.mcp_client import MCPManager

log = logging.getLogger(__name__)

# Lock to prevent concurrent git operations (e.g. background sync vs. active reads)
repo_lock = threading.Lock()


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

    with repo_lock:
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


def start_periodic_sync(
    *,
    mcp_manager: MCPManager | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
) -> threading.Thread:
    """Run clone_or_pull() in a background daemon thread on a timer.

    If *mcp_manager* and *loop* are provided, the repo overview is
    recomputed on the event loop after each successful sync.
    """
    settings = get_settings()
    interval = settings.sync_interval

    def _loop():
        while True:
            time.sleep(interval)
            try:
                result = clone_or_pull()
                log.info("[sync] %s", result)
                if mcp_manager is not None and loop is not None:
                    asyncio.run_coroutine_threadsafe(
                        mcp_manager.compute_overview(), loop
                    )
            except Exception:
                log.exception("[sync] Failed to sync repo")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info("[sync] Periodic repo sync every %ds", interval)
    return t
