"""Launch only verify check1's pytest child without its parent's live authority."""
from __future__ import annotations

import os
import pwd
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def _root_test_parent(parent: Path) -> bool:
    """Linux/root protocol fixtures execute files and enforce custody of all ancestors."""
    for directory in (parent, *parent.parents):
        metadata = directory.lstat()
        if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0
                or metadata.st_mode & 0o022):
            return False
    return not bool(os.statvfs(parent).f_flag & os.ST_NOEXEC)


def create_test_directory() -> Path:
    """Keep test-owned state outside the caller's deployment authority tree."""
    protected = [Path("/var/lib/dailycheck-deploy"), Path("/var/lib/dailycheck-prod-agent")]
    if configured := os.environ.get("DAILYCHECK_DEPLOY_STATE_DIR"):
        protected.append(Path(configured))
    protected = [path.resolve() for path in protected]
    parents = [Path(tempfile.gettempdir())]
    try:
        parents.append(Path(pwd.getpwuid(os.geteuid()).pw_dir))
    except KeyError:
        pass
    parents.append(Path("/tmp"))
    root_protocol = sys.platform == "linux" and os.geteuid() == 0
    for parent in dict.fromkeys(parents):
        try:
            parent = parent.resolve(strict=True)
            if any(parent.is_relative_to(root) for root in protected):
                continue
            if root_protocol and not _root_test_parent(parent):
                continue
            directory = Path(tempfile.mkdtemp(prefix="dailycheck-tests-", dir=parent)).resolve()
        except OSError:
            continue
        if any(directory.is_relative_to(root) or root.is_relative_to(directory)
               for root in protected):
            directory.rmdir()
            continue
        return directory
    raise OSError("isolated pytest directory unavailable")


def main() -> int:
    if sys.argv[1:] == ["--create-directory"]:
        print(create_test_directory())
        return 0
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
