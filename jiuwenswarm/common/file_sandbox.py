# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Single-root file boundary for model-visible JiuwenSwarm file operations."""

from __future__ import annotations

import os
import re
import shutil
import sys
import uuid
from pathlib import Path


FILE_SANDBOX_ROOT_ENV = "JIUWENSWARM_FILE_SANDBOX_ROOT"
RECEIVED_FILES_DIR_ENV = "JIUWENSWARM_RECEIVED_FILES_DIR"
_RECEIVED_DIR_NAME = "JiuwenSwarm"
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


class FileSandboxViolation(PermissionError):
    """Raised when a model-visible file path escapes the configured root."""


def _user_desktop_dir() -> Path:
    """Return the current user's Desktop folder."""
    if sys.platform == "win32":
        try:
            import ctypes

            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_DESKTOPDIRECTORY = 0x0010
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf) == 0:
                if buf.value:
                    return Path(buf.value)
        except (AttributeError, OSError, ValueError):
            pass
    xdg = os.getenv("XDG_DESKTOP_DIR", "").strip()
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / "Desktop"


def get_received_files_dir(*, create: bool = True) -> Path:
    """Directory where inbound transferred files are saved and later looked up.

    Defaults to ``<Desktop>/JiuwenSwarm``. Override with
    ``JIUWENSWARM_RECEIVED_FILES_DIR``.
    """
    configured = os.getenv(RECEIVED_FILES_DIR_ENV, "").strip()
    root = (
        Path(configured).expanduser().resolve(strict=False)
        if configured
        else (_user_desktop_dir() / _RECEIVED_DIR_NAME).resolve(strict=False)
    )
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def get_file_sandbox_root(*, create: bool = True) -> Path:
    """Return the model-visible file root.

    Defaults to the Desktop receive folder so transferred files are both
    saved and looked up in the same place. ``JIUWENSWARM_FILE_SANDBOX_ROOT``
    wins when set; otherwise ``JIUWENSWARM_RECEIVED_FILES_DIR`` is used.
    Broad roots such as the filesystem root, the user's home, or the whole
    Jiuwen workspace are rejected.
    """
    workspace = Path(
        os.getenv("JIUWENSWARM_WORKSPACE")
        or (Path.home() / ".jiuwenswarm")
    ).expanduser().resolve(strict=False)
    configured = (
        os.getenv(FILE_SANDBOX_ROOT_ENV, "").strip()
        or os.getenv(RECEIVED_FILES_DIR_ENV, "").strip()
    )
    root = (
        Path(configured).expanduser().resolve(strict=False)
        if configured
        else get_received_files_dir(create=False)
    )

    forbidden_roots = {
        Path(root.anchor),
        Path.home().expanduser().resolve(strict=False),
        workspace,
    }
    if root in forbidden_roots:
        raise FileSandboxViolation(f"file sandbox root is too broad: {root}")

    if create:
        root.mkdir(parents=True, exist_ok=True)
        desktop_inbox = (_user_desktop_dir() / _RECEIVED_DIR_NAME).resolve(strict=False)
        if root != desktop_inbox and sys.platform != "win32":
            try:
                os.chmod(root, 0o700)
            except OSError:
                pass
    return root


def describe_file_sandbox_dir() -> str:
    """Directory the model should use for transferred files.

    Relative file names resolve under this directory. Prompt text must keep
    working even if the configured root is rejected.
    """
    try:
        return str(get_file_sandbox_root(create=False))
    except OSError:
        return "桌面/JiuwenSwarm"


def _allowed_file_roots(*, create: bool = False) -> tuple[Path, ...]:
    sandbox = get_file_sandbox_root(create=True)
    received = get_received_files_dir(create=create)
    if received == sandbox:
        return (sandbox,)
    return (sandbox, received)


def resolve_file_sandbox_path(
    raw_path: str | os.PathLike[str],
    *,
    must_exist: bool = False,
    require_file: bool = False,
    create_parent: bool = False,
) -> Path:
    """Resolve a relative/absolute path and require it to remain under root."""
    text = str(raw_path or "").strip()
    if not text or "\x00" in text:
        raise FileSandboxViolation("file path is empty or contains NUL")

    root = get_file_sandbox_root()
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not any(_is_relative_to(resolved, allowed) for allowed in _allowed_file_roots()):
        raise FileSandboxViolation(
            f"file path is outside the JiuwenSwarm sandbox: {resolved}"
        )

    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"sandbox file does not exist: {resolved}")
    if require_file and not resolved.is_file():
        raise FileNotFoundError(f"not a sandbox file: {resolved}")
    if create_parent:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_allowed_delivery_path(
    raw_path: str | os.PathLike[str],
    *,
    extra_roots: tuple[str | os.PathLike[str] | None, ...] = (),
    must_exist: bool = False,
    require_file: bool = False,
) -> Path:
    """Resolve a file that may be delivered to the user or another device.

    The receive/sandbox directory and the active project or team workspace are
    both legal. Paths outside those roots are rejected.
    """
    text = str(raw_path or "").strip()
    if not text or "\x00" in text:
        raise FileSandboxViolation("file path is empty or contains NUL")

    roots: list[Path] = list(_allowed_file_roots())
    try:
        from jiuwenswarm.common.utils import (
            get_agent_sessions_dir,
            get_agent_workspace_dir,
            get_default_project_workspace_dir,
        )

        roots.extend(
            (
                get_agent_workspace_dir(),
                get_default_project_workspace_dir(),
                get_agent_sessions_dir(),
            )
        )
    except Exception:
        pass
    for extra in extra_roots:
        if not extra:
            continue
        roots.append(Path(extra).expanduser().resolve(strict=False))

    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved_root = root.expanduser().resolve(strict=False)
        key = str(resolved_root)
        if key in seen:
            continue
        seen.add(key)
        unique_roots.append(resolved_root)

    raw_candidate = Path(text).expanduser()
    candidates = [raw_candidate] if raw_candidate.is_absolute() else []
    if not raw_candidate.is_absolute():
        candidates.extend(root / raw_candidate for root in unique_roots)
    resolved: Path | None = None
    for candidate in candidates:
        current = candidate.resolve(strict=False)
        if any(_is_relative_to(current, root) for root in unique_roots):
            resolved = current
            break
    if resolved is None:
        raise FileSandboxViolation(
            f"file path is outside the receive directory and the current project: {text}"
        )
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"delivery file does not exist: {resolved}")
    if require_file and not resolved.is_file():
        raise FileNotFoundError(f"not a delivery file: {resolved}")
    return resolved


def is_file_sandbox_path(raw_path: str | os.PathLike[str]) -> bool:
    """Return whether a path resolves inside the model-visible sandbox."""
    try:
        resolve_file_sandbox_path(raw_path)
    except (FileSandboxViolation, OSError):
        return False
    return True


def sandbox_subdir(*parts: str) -> Path:
    """Create and return a private directory below the sandbox root."""
    relative = Path(*parts)
    if relative.is_absolute() or ".." in relative.parts:
        raise FileSandboxViolation(f"invalid sandbox subdirectory: {relative}")
    directory = resolve_file_sandbox_path(relative)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def safe_sandbox_filename(filename: str, *, fallback: str = "file") -> str:
    """Normalize an untrusted filename to one path component."""
    name = Path(str(filename or "")).name.strip()
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).rstrip(". ").strip()
    if not name or name in {".", ".."}:
        name = fallback
    encoded = name.encode("utf-8")
    if len(encoded) > 180:
        name = encoded[:180].decode("utf-8", "ignore") or fallback
    return name


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _unique_leaf_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 10_000):
        candidate = directory / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{stem}-{uuid.uuid4().hex[:8]}{suffix}"


def persist_received_file(
    data: bytes,
    *,
    filename: str,
    max_bytes: int | None = None,
) -> Path:
    """Save a transferred file under the user-visible Desktop receive folder."""
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"received file exceeds sandbox limit ({len(data)} > {max_bytes})")
    directory = get_received_files_dir()
    destination = _unique_leaf_path(directory, safe_sandbox_filename(filename))
    with destination.open("xb") as handle:
        handle.write(data)
    return destination


def persist_sandbox_bytes(
    data: bytes,
    *,
    filename: str,
    subdir: tuple[str, ...] = ("inbox",),
    max_bytes: int | None = None,
) -> Path:
    """Persist received bytes under the sandbox using a non-guessable name."""
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"received file exceeds sandbox limit ({len(data)} > {max_bytes})")
    directory = sandbox_subdir(*subdir)
    safe_name = safe_sandbox_filename(filename)
    destination = directory / f"{uuid.uuid4().hex[:12]}-{safe_name}"
    with destination.open("xb") as handle:
        handle.write(data)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return destination


def copy_file_into_sandbox(
    source: str | os.PathLike[str],
    *,
    filename: str | None = None,
    subdir: tuple[str, ...] = ("inbox",),
    max_bytes: int | None = None,
) -> Path:
    """Copy one explicitly supplied local file into the model-visible sandbox."""
    source_path = Path(source).expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise FileNotFoundError(f"not a regular file: {source_path}")
    size = source_path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise ValueError(f"received file exceeds sandbox limit ({size} > {max_bytes})")
    directory = sandbox_subdir(*subdir)
    safe_name = safe_sandbox_filename(filename or source_path.name)
    destination = directory / f"{uuid.uuid4().hex[:12]}-{safe_name}"
    with source_path.open("rb") as source_handle, destination.open("xb") as target_handle:
        shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return destination


__all__ = [
    "FILE_SANDBOX_ROOT_ENV",
    "RECEIVED_FILES_DIR_ENV",
    "FileSandboxViolation",
    "copy_file_into_sandbox",
    "describe_file_sandbox_dir",
    "get_file_sandbox_root",
    "get_received_files_dir",
    "is_file_sandbox_path",
    "persist_received_file",
    "persist_sandbox_bytes",
    "resolve_file_sandbox_path",
    "safe_sandbox_filename",
    "sandbox_subdir",
]
