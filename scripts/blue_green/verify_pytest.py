"""Launch only verify check1's pytest child without its parent's live authority."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    directory = Path(sys.argv[1]).absolute()
    env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
           if key in os.environ}
    env.update(
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", os.defpath),
        HOME=str(directory / "home"), TMPDIR=str(directory),
        XDG_CONFIG_HOME=str(directory / "config"), XDG_CACHE_HOME=str(directory / "cache"),
        XDG_DATA_HOME=str(directory / "data"), RUFF_CACHE_DIR=str(directory / "ruff"),
        PYTHONPATH=os.getcwd(), PYTHONNOUSERSITE="1", PYTHONSAFEPATH="1",
        PYTHONDONTWRITEBYTECODE="1", PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
        DAILYCHECK_DB_DIR=str(directory),
        DAILYCHECK_DEPLOY_STATE_DIR=str(directory / "state"),
        DAILYCHECK_DEPLOY_EVENT_LOG=str(directory / "events.jsonl"),
        DAILYCHECK_PROD_AGENT_INBOX=str(directory / "inbox.jsonl"),
        DAILYCHECK_BG_LOCK_FILE=str(directory / "bluegreen.lock"),
        DAILYCHECK_PROD_REPO=str(directory / "absent-repo"),
        DAILYCHECK_GITEA_TOKEN_FILE=str(directory / "absent-token"),
        GITEA_TOKEN_FILE=str(directory / "absent-token"),
        REPORT_DIR=str(directory / "reports"), BACKUP_DIR=str(directory / "backups"),
    )
    Path(env["HOME"]).mkdir(mode=0o700)
    # close_fds is essential: removing the descriptor names would leave kernel authority.
    return subprocess.call([sys.executable, "-B", "-m", "pytest", *sys.argv[2:]],
                           env=env, close_fds=True, umask=0o022)


if __name__ == "__main__":
    raise SystemExit(main())
