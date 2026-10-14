"""Folder completion for typed paths: what Tab offers and which part of the text is wrong."""

import os
from pathlib import Path


def split_path(text: str) -> tuple[str, str]:
    """Split typed text into the folder part (up to the last "/") and the name being typed.

    Text with no "/" (including "" and "~") is a name in the home folder: ("~/", name).
    """
    if text == "~":
        return "~/", ""
    if "/" not in text:
        return "~/", text
    cut = text.rindex("/") + 1
    return text[:cut], text[cut:]


def to_path(text: str) -> Path | None:
    """The folder typed text names, by the same rules as completion: a bare name is in home.

    None when it can't be worked out, e.g. "~nobody/" for a user that doesn't exist.
    """
    parent, name = split_path(text.strip())
    try:
        return (Path(parent).expanduser() / name).resolve()
    except (RuntimeError, OSError):
        return None


def _folder(parent: str) -> Path | None:
    try:
        return Path(parent).expanduser()
    except RuntimeError:  # "~name/" for a user that doesn't exist
        return None


def subfolders(parent: str, prefix: str) -> list[str]:
    """Names of the folders in `parent` that start with `prefix`, A to Z.

    Hidden folders are offered only when the prefix starts with ".", as in a shell.
    Raises OSError if `parent` isn't a readable folder.
    """
    folder = _folder(parent)
    if folder is None:
        raise FileNotFoundError(parent)
    with os.scandir(folder) as entries:
        names = [
            e.name for e in entries
            if e.name.startswith(prefix) and (prefix.startswith(".") or not e.name.startswith("."))
            and e.is_dir()  # follows symlinks: a link to a folder counts
        ]
    return sorted(names, key=lambda n: (n.lower(), n))


def problem_span(text: str) -> tuple[int, int] | None:
    """The part of `text` to show in red, as (start, end); None if it's fine.

    The folder part if it isn't an existing folder, else the name being typed if no folder
    in it starts with that name. An empty name (just after a "/") is always fine.
    """
    parent, prefix = split_path(text)
    start = len(text) - len(prefix)  # where the name starts in the text as typed
    whole_parent = (0, start) if start else (0, len(text))  # no "/" typed: the parent is home
    folder = _folder(parent)
    if folder is None or not folder.is_dir():
        return whole_parent
    if prefix in ("", ".", ".."):  # "." and ".." are folders, though never listed
        return None
    try:
        return None if subfolders(parent, prefix) else (start, len(text))
    except OSError:
        return whole_parent  # e.g. no permission to list it


class Completer:
    """Tab completion that cycles: each press fills in the next folder matching the prefix.

    It cycles while the text is still what it last filled in; any edit starts a new round
    (the input calls reset(), so retyping the same text counts as an edit too).
    """

    def __init__(self):
        self.parent = ""
        self.matches: list[str] = []
        self.index = 0
        self.filled: str | None = None

    def reset(self) -> None:
        self.filled = None

    def complete(self, text: str) -> str:
        """The text after one Tab press: the next match, or `text` unchanged if none."""
        if text == self.filled and self.matches:
            self.index = (self.index + 1) % len(self.matches)
        else:
            self.parent, prefix = split_path(text)
            try:
                self.matches = subfolders(self.parent, prefix)
            except OSError:
                self.matches = []
            self.index = 0
            if not self.matches:
                self.filled = None
                return text
        self.filled = self.parent + self.matches[self.index]
        return self.filled
