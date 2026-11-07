"""Find the Claude Code config folders on this machine, and the account each uses.

Each `claude` login lives in its own config folder: ~/.claude by default, or the
folder in CLAUDE_CONFIG_DIR. Folders may share their `projects/` through a symlink, so
one transcript can be resumed with any of those accounts.
"""

import glob
import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
PATTERN_ENV = "CC_SESSIONS_MAN_CLAUDE_DIRS_PATTERN"
DEFAULT_PATTERN = "~/.claude*"


@dataclass(frozen=True)
class ConfigDir:
    path: Path  # absolute, symlinks resolved
    account_uuid: str = ""  # "" when not logged in with a claude.ai account
    projects: Path | None = None  # the real projects folder, symlinks resolved
    projects_linked: bool = False  # projects/ is a symlink to another folder's


def settings_files(path: Path) -> list[Path]:
    """Where Claude Code keeps a config folder's settings, including its account.

    A CLAUDE_CONFIG_DIR folder keeps them inside, in .claude.json. The default folder
    keeps them beside it, in ~/.claude.json, unless CLAUDE_CONFIG_DIR names it
    explicitly, which makes Claude Code use ~/.claude/.claude.json instead.
    """
    if path == DEFAULT_CLAUDE_HOME.resolve():
        return [DEFAULT_CLAUDE_HOME.parent / ".claude.json", path / ".claude.json"]
    return [path / ".claude.json"]


# Settings files are read again only when they change: (path, size, mtime) -> uuid.
_accounts: dict[tuple[str, int, int], str] = {}


def _account_in(f: Path) -> str:
    try:
        st = f.stat()
        key = (str(f), st.st_size, st.st_mtime_ns)
        if key not in _accounts:
            data = json.loads(f.read_text())
            account = data.get("oauthAccount") if isinstance(data, dict) else None
            uuid = account.get("accountUuid") if isinstance(account, dict) else None
            _accounts[key] = uuid if isinstance(uuid, str) else ""
        return _accounts[key]
    except OSError, ValueError:
        return ""


def account_of(path: Path) -> str:
    """The claude.ai account a config folder is logged in with, or ""."""
    for f in settings_files(path):
        if uuid := _account_in(f):
            return uuid
    return ""


def config_dir(path: str | Path) -> ConfigDir:
    path = Path(path).expanduser().resolve()
    projects = path / "projects"
    return ConfigDir(
        path=path,
        account_uuid=account_of(path),
        projects=projects.resolve() if projects.is_dir() else None,
        projects_linked=projects.is_symlink(),
    )


def discover(pattern: str | None = None) -> list[ConfigDir]:
    """Every config folder matching `pattern` (default: $CC_SESSIONS_MAN_CLAUDE_DIRS_
    PATTERN, else ~/.claude*), plus $CLAUDE_CONFIG_DIR if it's set. Files that match,
    such as ~/.claude.json, are skipped, and a folder reached twice counts once.
    """
    pattern = pattern or os.environ.get(PATTERN_ENV, "").strip() or DEFAULT_PATTERN
    found = [p for p in glob.glob(os.path.expanduser(pattern)) if is_config_dir(p)]
    if env := os.environ.get("CLAUDE_CONFIG_DIR", "").strip():
        found.append(env)  # named on purpose, so taken as it is
    dirs: dict[Path, ConfigDir] = {}
    for p in sorted(found):
        if os.path.isdir(os.path.expanduser(p)):
            d = config_dir(p)
            dirs.setdefault(d.path, d)
    return list(dirs.values())


def is_config_dir(p: str) -> bool:
    """Whether a folder the pattern matched looks like a Claude Code config folder,
    not one another tool named ~/.claude-something.
    """
    path = Path(p).expanduser().resolve()
    return path.is_dir() and (
        (path / "projects").is_dir() or any(f.exists() for f in settings_files(path))
    )


def with_discovered(dirs: list[ConfigDir]) -> list[ConfigDir]:
    """`dirs` plus every other config folder found: what running checks and deletes
    look through, since a session read from one folder may be open, or keep backups,
    under another sharing its projects.
    """
    seen = {d.path for d in dirs}
    return dirs + [d for d in discover() if d.path not in seen]


def project_roots(dirs: list[ConfigDir]) -> list[tuple[Path, ConfigDir]]:
    """Each real projects folder once, with the config folder that owns it: the one
    whose projects/ is the folder itself rather than a symlink to it. A folder only
    reached through symlinks belongs to the first folder linking to it.
    """
    roots: dict[Path, ConfigDir] = {}
    for d in sorted(dirs, key=lambda d: d.projects_linked):  # real folders first
        if d.projects is not None:
            roots.setdefault(d.projects, d)
    return list(roots.items())


def owner_of(account_uuid: str, root_owner: ConfigDir, dirs: list[ConfigDir]) -> Path:
    """The config folder a session belongs to: the one logged in with the account in
    its transcript, or else the folder owning its projects folder. Only folders that
    share that projects folder count, since claude --resume looks for the transcript
    in its own projects.
    """
    if account_uuid and account_uuid != root_owner.account_uuid:
        for d in dirs:
            if d.account_uuid == account_uuid and d.projects == root_owner.projects:
                return d.path
    return root_owner.path
