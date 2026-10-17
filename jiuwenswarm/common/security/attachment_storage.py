# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Create exclusive attachment files without following workspace child links."""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

from jiuwenswarm.common.security.attachment_download import AttachmentError


def validate_attachment_name(name: str) -> str:
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³")
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or name.endswith((" ", "."))
        or any(ord(c) < 32 or ord(c) == 127 or c in '/\\:<>"|?*' for c in name)
        or name.split(".")[0].rstrip().upper() in reserved
    ):
        raise AttachmentError(
            "Attachment name must be a plain filename", "ATTACHMENT_INVALID_NAME"
        )
    try:
        name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AttachmentError(
            "Attachment name contains invalid Unicode", "ATTACHMENT_INVALID_NAME"
        ) from exc
    return name


def _windows_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


@contextlib.contextmanager
def _locked_windows_directory(path: Path) -> Iterator[None]:
    """Refuse reparse points and prevent directory replacement while writing children."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    class AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]

    # FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES. Attribute-only handles do
    # not enforce the same sharing restrictions; include directory read access.
    # Share read only: also prevent opening a directory for reparse-point
    # mutation after inspection, not just renaming/removing the directory.
    # OPEN_EXISTING; BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
    handle = kernel.CreateFileW(
        _windows_path(path), 0x81, 0x1, None, 3, 0x02200000, None
    )
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = AttributeTagInfo()
        if not kernel.GetFileInformationByHandleEx(
            handle, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & 0x400 or not info.attributes & 0x10:
            raise AttachmentError(
                "Attachment directory must not be a link or reparse point"
            )
        yield
    finally:
        kernel.CloseHandle(handle)


@contextlib.contextmanager
def _windows_attachment(root: Path, name: str, content: bytes) -> Iterator[Path]:
    # Holding every directory below the trusted root prevents a junction or
    # rename race between checking a parent and opening the output file.
    with _locked_windows_directory(root):
        parent = root / "attachments"
        # Inherit the workspace ACL, including the sandbox account's access.
        # Python 3.13 turns mode=0700 into a private Windows DACL.
        os.makedirs(_windows_path(parent), exist_ok=True)
        with _locked_windows_directory(parent):
            directory = parent / uuid.uuid4().hex
            os.mkdir(_windows_path(directory))
            committed = False
            try:
                with _locked_windows_directory(directory):
                    target = directory / name
                    try:
                        with open(_windows_path(target), "xb") as output:
                            output.write(content)
                        yield target
                        committed = True
                    finally:
                        if not committed:
                            with contextlib.suppress(OSError):
                                os.unlink(_windows_path(target))
            finally:
                if not committed:
                    with contextlib.suppress(OSError):
                        os.rmdir(_windows_path(directory))


@contextlib.contextmanager
def _posix_attachment(root: Path, name: str, content: bytes) -> Iterator[Path]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    with contextlib.ExitStack() as handles:
        root_fd = os.open(root, flags)
        handles.callback(os.close, root_fd)
        try:
            os.mkdir("attachments", mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            pass
        parent_fd = os.open("attachments", flags, dir_fd=root_fd)
        handles.callback(os.close, parent_fd)
        directory = uuid.uuid4().hex
        os.mkdir(directory, mode=0o700, dir_fd=parent_fd)
        committed = False
        try:
            with contextlib.ExitStack() as leaf_handles:
                leaf_fd = os.open(directory, flags, dir_fd=parent_fd)
                leaf_handles.callback(os.close, leaf_fd)
                try:
                    file_fd = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=leaf_fd,
                    )
                    with os.fdopen(file_fd, "wb") as output:
                        output.write(content)
                    yield root / "attachments" / directory / name
                    committed = True
                finally:
                    if not committed:
                        with contextlib.suppress(OSError):
                            os.unlink(name, dir_fd=leaf_fd)
        finally:
            if not committed:
                with contextlib.suppress(OSError):
                    os.rmdir(directory, dir_fd=parent_fd)


@contextlib.contextmanager
def save_attachment(workspace: Path, name: str, content: bytes) -> Iterator[Path]:
    """Keep the file on successful request completion, clean it on error/cancellation."""
    validate_attachment_name(name)
    workspace.mkdir(parents=True, exist_ok=True)
    # The configured workspace is trusted; its children are not.
    root = workspace.resolve(strict=True)
    writer = _windows_attachment if os.name == "nt" else _posix_attachment
    with writer(root, name, content) as target:
        yield target
