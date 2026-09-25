"""Filesystem primitives for durable immutable publication."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def fsync_directory(path: Path) -> None:
    """Persist directory-entry changes below ``path``.

    Example: ``fsync_directory(Path("logs/events"))``.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ensure_durable_directory(path: Path) -> Path:
    """Create a directory chain and persist every new parent entry.

    Example: ``ensure_durable_directory(Path("logs/events/2026"))``.
    """
    destination = Path(path)
    missing: list[Path] = []
    current = destination
    while not current.exists():
        missing.append(current)
        current = current.parent
    if not current.is_dir():
        raise NotADirectoryError(
            f"invalid directory path {current}; expected existing directory"
        )
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
        fsync_directory(directory.parent)
    return destination


def _rename_directory_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for no-replace publication")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), destination)
    raise OSError(error_number, os.strerror(error_number), f"{source} -> {destination}")


def validate_persistence_id(value: str) -> str:
    """Validate one identifier before using it as a path component.

    Example: ``validate_persistence_id("run-001")``.
    """
    if not _SAFE_ID.fullmatch(value):
        raise ValueError(
            f"invalid persistence id {value!r}; expected path-safe ASCII identifier"
        )
    return value


def fsync_file(path: Path) -> None:
    """Flush one already-closed file before publishing its parent directory.

    Example: ``fsync_file(Path("video.mp4"))``.
    """
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Publish one JSON document without replacing an existing final name.

    Example: ``atomic_write_json(Path("event.json"), {"kind": "start"})``.
    """
    ensure_durable_directory(path.parent)
    if path.exists():
        raise FileExistsError(f"immutable destination already exists: {path}")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as sink:
            sink.write(encoded)
            sink.flush()
            os.fsync(sink.fileno())
        os.link(temporary, path)
        temporary.unlink()
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def publish_directory(source: Path, destination: Path) -> None:
    """Publish a flushed partial directory under a new immutable name.

    Example: ``publish_directory(Path("000001.partial"), Path("000001"))``.
    """
    if source.parent != destination.parent:
        raise ValueError("partial and final directories must share the same parent")
    ensure_durable_directory(destination.parent)
    fsync_directory(source)
    _rename_directory_noreplace(source, destination)
    fsync_directory(destination.parent)
