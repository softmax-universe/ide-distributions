"""Safe file writes for the brain: no symlink components, stay inside the root, atomic, locked."""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


class UnsafePath(ValueError):
    """A write was refused: a path component is a symlink or the path leaves its root."""


def check(root, path) -> Path:
    """``path`` made absolute, refused when it leaves ``root`` or any component below ``root`` is
    a symlink (a dangling one included). ``root`` itself may be a symlink (a mounted workspace)."""
    root, path = Path(root), Path(path)
    if not path.is_absolute():
        path = root / path
    try:
        rel = path.relative_to(root)
    except ValueError:
        raise UnsafePath(f"{path} is outside {root}") from None
    if ".." in rel.parts:
        raise UnsafePath(f"{path} contains '..'")
    current = root
    for part in rel.parts:
        current = current / part
        if os.path.islink(current):
            raise UnsafePath(f"{current} is a symlink")
    resolved_root = root.resolve()
    try:
        current.resolve().relative_to(resolved_root)
    except ValueError:
        raise UnsafePath(f"{path} resolves outside {root}") from None
    return path


def write(root, path, data: bytes | str, mode: int | None = None) -> Path:
    """Atomically write ``data``: a temp file in the same directory, then ``os.replace``."""
    path = check(root, path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    check(root, path)  # the directories just made are ours, but re-check the final location
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, flags, 0o644 if mode is None else mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    if mode is not None:
        os.chmod(path, mode)
    return path


@contextlib.contextmanager
def lock(brain_dir) -> Iterator[None]:
    """An exclusive flock on ``<brain_dir>/.lock`` (one tidy or install at a time)."""
    brain_dir = Path(brain_dir)
    brain_dir.mkdir(parents=True, exist_ok=True)
    with open(brain_dir / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
