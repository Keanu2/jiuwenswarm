"""Cross-platform private file helpers for configure scripts."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path


def default_a2a_service_id() -> str:
    """Return the default local A2A Registry service id for this host."""
    if sys.platform == "win32":
        return "JiuwenSwarm-Windows"
    if sys.platform == "darwin":
        return "JiuwenSwarm-Mac"
    return "JiuwenSwarm-Linux"


def _apply_private_mode(path: Path | int, mode: int) -> None:
    """Best-effort POSIX mode bits; no-op on Windows."""
    try:
        if isinstance(path, int):
            if hasattr(os, "fchmod"):
                os.fchmod(path, mode)
        else:
            os.chmod(path, mode)
    except OSError:
        pass


def ensure_private_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    resolved.mkdir(parents=True, exist_ok=True)
    _apply_private_mode(resolved, 0o700)
    return resolved


def write_private_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        _apply_private_mode(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary_name, path)
        _apply_private_mode(path, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def link_or_copy_file(source: Path, destination: Path) -> str:
    """Link source into destination when possible; otherwise copy."""
    source = source.expanduser().resolve(strict=True)
    if destination.is_symlink():
        if destination.resolve(strict=False) != source:
            destination.unlink()
    elif destination.exists():
        raise ValueError(f"refusing to replace non-symlink file: {destination}")
    if destination.exists():
        return "existing"
    try:
        destination.symlink_to(source)
        return "symlink"
    except (OSError, NotImplementedError):
        shutil.copy2(source, destination)
        _apply_private_mode(destination, 0o600)
        return "copy"


def credential_storage_label() -> str:
    if os.name == "nt":
        return "plaintext_env_current_user"
    return "plaintext_env_mode_0600"
