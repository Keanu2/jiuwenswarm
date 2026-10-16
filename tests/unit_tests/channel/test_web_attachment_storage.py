# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Cross-platform attachment storage and WebChannel request-boundary checks."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from types import SimpleNamespace
from jiuwenswarm.common.security.web_identity import PRINCIPAL_ATTR, WebPrincipal


import pytest

from jiuwenswarm.common.security.attachment_download import AttachmentError
from jiuwenswarm.common.security.attachment_storage import (
    save_attachment,
    validate_attachment_name,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.web import web_connect
from jiuwenswarm.gateway.channel_manager.web.web_connect import (
    WebChannel,
    WebChannelConfig,
)


def authenticated_socket():
    return SimpleNamespace(**{PRINCIPAL_ATTR: WebPrincipal("local", True)})


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for name in (
        "WEB_ATTACHMENT_PRIVATE_ORIGINS",
        "WEB_ATTACHMENT_MAX_BYTES",
        "WEB_ATTACHMENT_TIMEOUT_SECONDS",
        "WEB_ATTACHMENT_MAX_FILES",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize(
    "name",
    [
        "../../etc/cron.d/evil",
        "../config.yaml",
        "..\\config.yaml",
        "/tmp/evil",
        "C:\\evil",
        "C:evil",
        "\\\\server\\share\\evil",
        "\\\\?\\C:\\evil",
        "x.txt:stream",
        ".",
        "..",
        "CON",
        "NUL.txt",
        "COM1",
        "COM¹.txt",
        "LPT9.log",
        "name.",
        "name ",
        "a\x00b",
        "name\n.txt",
        "sub/file.txt",
        "sub\\file.txt",
        "",
        None,
        123,
        "bad\ud800.txt",
    ],
)
def test_names_cannot_be_paths_or_windows_devices(name):
    with pytest.raises(AttachmentError) as error:
        validate_attachment_name(name)
    assert error.value.code == "ATTACHMENT_INVALID_NAME"


def test_valid_names_and_duplicate_uploads_never_overwrite_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "报表 one.xlsx"
    existing.write_bytes(b"existing")
    with save_attachment(workspace, existing.name, b"one") as first:
        assert first.is_relative_to(workspace / "attachments")
        assert first.name == existing.name
    with save_attachment(workspace, existing.name, b"two") as second:
        assert second != first
    assert first.read_bytes() == b"one"
    assert second.read_bytes() == b"two"
    assert existing.read_bytes() == b"existing"


@pytest.mark.parametrize(
    "error", [RuntimeError("later attachment failed"), asyncio.CancelledError()]
)
def test_failed_or_cancelled_request_removes_its_partial_attachments(tmp_path, error):
    with pytest.raises(type(error)):
        with save_attachment(tmp_path, "report.txt", b"partial") as path:
            assert path.read_bytes() == b"partial"
            raise error
    assert not path.exists()
    assert list((tmp_path / "attachments").iterdir()) == []


def _make_directory_link(link, destination):
    if os.name == "nt":
        # mklink /J creates only this test's directory junction; it does not
        # move/delete either resolved workspace path and needs no symlink privilege.
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(destination)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
    else:
        link.symlink_to(destination, target_is_directory=True)


def test_existing_symlink_or_windows_junction_is_never_followed(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "attachments"
    _make_directory_link(link, outside)
    try:
        with pytest.raises((AttachmentError, OSError)):
            with save_attachment(workspace, "evil.txt", b"must-not-escape"):
                pytest.fail("Linked attachment directory was accepted")
        assert list(outside.iterdir()) == []
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove the junction itself, never recurse into its target.
        else:
            link.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory sharing semantics")
def test_windows_directory_cannot_be_replaced_during_attachment_write(tmp_path):
    with save_attachment(tmp_path, "report.txt", b"data") as path:
        with pytest.raises(OSError):
            path.parent.rename(tmp_path / "moved-leaf")
        with pytest.raises(OSError):
            (tmp_path / "attachments").rename(tmp_path / "moved-parent")
    assert path.read_bytes() == b"data"


@pytest.mark.skipif(
    os.name != "nt", reason="Windows reparse mutation sharing protection"
)
def test_windows_directory_cannot_be_opened_for_reparse_mutation(tmp_path):
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
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    with save_attachment(tmp_path, "report.txt", b"data") as path:
        handle = kernel.CreateFileW(
            str(path.parent), 0x40000000, 0x7, None, 3, 0x02000000, None
        )
        if handle != wintypes.HANDLE(-1).value:
            kernel.CloseHandle(handle)
            pytest.fail("Opened a held directory with write access")
        assert ctypes.get_last_error() == 32  # ERROR_SHARING_VIOLATION


@pytest.mark.skipif(os.name != "nt", reason="Windows extended path handling")
def test_windows_download_path_can_exceed_max_path(tmp_path):
    workspace = tmp_path / ("parent-" + "x" * 80)
    name = "report-" + "y" * 120 + ".xlsx"
    with save_attachment(workspace, name, b"long-path-report") as path:
        assert len(str(path)) > 260
    from jiuwenswarm.common.security.attachment_storage import _windows_path

    with open(_windows_path(path), "rb") as saved:
        assert saved.read() == b"long-path-report"


@pytest.mark.skipif(os.name != "nt", reason="Windows workspace ACL inheritance")
def test_windows_attachment_inherits_workspace_read_permissions(tmp_path):
    win32security = pytest.importorskip("win32security")
    import win32con
    import ntsecuritycon

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Use a test-only inheritable reader ACE rather than changing/impersonating
    # a real sandbox account. A private 0700 DACL would discard this grant.
    reader = win32security.ConvertStringSidToSid("S-1-5-32-545")
    security = win32security.GetFileSecurity(
        str(workspace), win32security.DACL_SECURITY_INFORMATION
    )
    acl = security.GetSecurityDescriptorDacl()
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE,
        ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_EXECUTE,
        reader,
    )
    win32security.SetNamedSecurityInfo(
        str(workspace),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
    with save_attachment(workspace, "report.txt", b"sandbox-readable") as path:
        for target in (path.parent.parent, path.parent, path):
            inherited = win32security.GetFileSecurity(
                str(target), win32security.DACL_SECURITY_INFORMATION
            )
            entries = inherited.GetSecurityDescriptorDacl()
            assert any(
                entries.GetAce(index)[2] == reader
                and entries.GetAce(index)[0][1] & 0x10  # INHERITED_ACE
                for index in range(entries.GetAceCount())
            ), f"Workspace reader permission was lost on {target.name}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory FD / O_NOFOLLOW")
def test_posix_raced_directory_link_is_not_followed(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    real_open = os.open
    raced = []

    def race_open(path, flags, *args, **kwargs):
        if (
            isinstance(path, str)
            and len(path) == 32
            and kwargs.get("dir_fd") is not None
        ):
            parent_fd = kwargs["dir_fd"]
            os.rmdir(path, dir_fd=parent_fd)
            os.symlink(str(outside), path, dir_fd=parent_fd)
            raced.append(tmp_path / "workspace" / "attachments" / path)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race_open)
    try:
        with pytest.raises(OSError):
            with save_attachment(tmp_path / "workspace", "evil.txt", b"denied"):
                pytest.fail("Followed a raced symlink")
        assert list(outside.iterdir()) == []
    finally:
        for link in raced:
            link.unlink()


@pytest.fixture
def channel(monkeypatch, tmp_path):
    monkeypatch.setattr(web_connect, "get_agent_workspace_dir", lambda: tmp_path)
    instance = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    instance.send_response = AsyncMock()
    return instance


def request(method="chat.send", files=None):
    return json.dumps(
        {
            "type": "req",
            "id": "attachment-test",
            "method": method,
            "params": {"content": "hello", "files": files},
        }
    )


@pytest.mark.asyncio
async def test_normal_url_download_returns_local_path_and_preserves_filename(channel):
    channel._download_file = AsyncMock(return_value=b"report")
    original = {
        "content": "hi",
        "files": [
            {"url": "https://files.example/a?sig=token", "name": "报表 one.xlsx"}
        ],
    }
    result = await channel._process_files(original)
    item = result["files"][0]
    assert Path(item["path"]).read_bytes() == b"report"
    assert Path(item["path"]).name == "报表 one.xlsx"
    assert item["name"] == "报表 one.xlsx"
    assert "url" not in item and "uri" not in item
    assert "path" not in original["files"][0]


@pytest.mark.asyncio
async def test_downloaded_path_cannot_be_overridden_by_client_metadata(channel):
    from jiuwenswarm.agents.harness.common.prompt.user_prompt_builder import (
        extract_current_turn_attachments,
    )

    channel._download_file = AsyncMock(return_value=b"report")
    result = await channel._process_files(
        {
            "files": [
                {
                    "url": "https://files.example/a",
                    "name": "report.xlsx",
                    "original_path": "/untrusted/other",
                    "mediaPath": "/untrusted/alias",
                    "text_path": "/untrusted/text",
                    "size_bytes": 123,
                }
            ]
        }
    )
    normalized = extract_current_turn_attachments(result)
    assert normalized[0]["path"] == result["files"][0]["path"]
    assert normalized[0]["filename"] == "report.xlsx"
    assert normalized[0]["size_bytes"] == len(b"report")
    assert "text_path" not in normalized[0]


@pytest.mark.asyncio
async def test_uri_filename_aliases_and_empty_download_are_supported(channel):
    channel._download_file = AsyncMock(return_value=b"")
    result = await channel._process_files(
        {"files": [{"uri": "https://files.example/a", "filename": "empty.txt"}]}
    )
    assert Path(result["files"][0]["path"]).read_bytes() == b""
    assert "uri" not in result["files"][0]


@pytest.mark.asyncio
async def test_multiple_same_named_attachments_can_be_saved_in_one_request(channel):
    channel._download_file = AsyncMock(side_effect=[b"first", b"second"])
    result = await channel._process_files(
        {
            "files": [
                {"url": "https://files.example/first", "name": "report.xlsx"},
                {"url": "https://files.example/second", "name": "report.xlsx"},
            ]
        }
    )
    first, second = (Path(item["path"]) for item in result["files"])
    assert first != second
    assert first.read_bytes() == b"first"
    assert second.read_bytes() == b"second"


@pytest.mark.asyncio
async def test_current_upload_metadata_and_local_path_references_are_unchanged(channel):
    channel._download_file = AsyncMock(
        side_effect=AssertionError("Must not fetch uploaded files")
    )
    grouped = {
        "files": {
            "uploaded_documents": [
                {"path": "/existing/report.pdf", "filename": "report.pdf"}
            ]
        }
    }
    assert await channel._process_files(grouped) is grouped
    local = {"files": [{"path": "/existing/report.pdf", "name": "report.pdf"}]}
    assert await channel._process_files(local) == local
    channel._download_file.assert_not_awaited()


@pytest.mark.parametrize(
    "method", ["media.persist", "document.persist", "history.get", "unknown.method"]
)
@pytest.mark.asyncio
async def test_unrelated_methods_cannot_trigger_url_downloads(channel, method):
    channel._download_file = AsyncMock(
        side_effect=AssertionError("No I/O for this method")
    )
    callback = AsyncMock()
    channel.on_message(callback)
    await channel._handle_raw_message(
        authenticated_socket(), request(method, [{"url": "https://files.example/a"}]), {}
    )
    channel._download_file.assert_not_awaited()
    callback.assert_not_awaited()
    assert channel.send_response.call_args.kwargs["code"] == "ATTACHMENT_REJECTED"


@pytest.mark.parametrize(
    "method,params",
    [
        (
            "media.persist",
            {"media_items": [{"filename": "image.png", "base64_data": "aGVsbG8="}]},
        ),
        (
            "document.persist",
            {"documents": [{"filename": "report.pdf", "base64_data": "aGVsbG8="}]},
        ),
        (
            "chat.send",
            {
                "files": {
                    "uploaded_documents": [
                        {"filename": "report.pdf", "path": "/existing/report.pdf"}
                    ]
                }
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_normal_upload_and_persist_requests_still_reach_callback(
    channel, method, params
):
    channel._download_file = AsyncMock(side_effect=AssertionError("No URL download"))
    callback = AsyncMock(return_value=True)
    channel.on_message(callback)
    raw = json.dumps(
        {"type": "req", "id": "req-persist", "method": method, "params": params}
    )
    await channel._handle_raw_message(authenticated_socket(), raw, {})
    callback.assert_awaited_once()
    assert callback.call_args.args[0].params == params
    channel._download_file.assert_not_awaited()


@pytest.mark.parametrize(
    "file_info,code",
    [
        (
            {"url": "https://files.example/a", "name": "../../evil"},
            "ATTACHMENT_INVALID_NAME",
        ),
        (
            {"url": "http://169.254.169.254/a", "name": "report.txt"},
            "ATTACHMENT_REJECTED",
        ),
        ({"url": "file:///etc/passwd", "name": "report.txt"}, "ATTACHMENT_REJECTED"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_attachment_rejected_before_network_callback_or_agent(
    channel, file_info, code
):
    channel._download_file = AsyncMock(
        side_effect=AssertionError("Input must be rejected first")
    )
    callback = AsyncMock()
    channel.on_message(callback)
    await channel._handle_raw_message(authenticated_socket(), request(files=[file_info]), {})
    assert channel.send_response.call_args.kwargs["code"] == code
    channel._download_file.assert_not_awaited()
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_download_cleans_earlier_files_and_does_not_forward_raw_urls(
    channel, tmp_path, caplog
):
    channel._download_file = AsyncMock(
        side_effect=[
            b"first",
            AttachmentError("Download failed", "ATTACHMENT_DOWNLOAD_FAILED"),
        ]
    )
    callback = AsyncMock()
    channel.on_message(callback)
    files = [
        {"url": f"https://files.example/{i}?secret=do-not-log", "name": f"{i}.txt"}
        for i in range(2)
    ]
    await channel._handle_raw_message(authenticated_socket(), request(files=files), {})
    assert (
        channel.send_response.call_args.kwargs["code"] == "ATTACHMENT_DOWNLOAD_FAILED"
    )
    assert list((tmp_path / "attachments").iterdir()) == []
    callback.assert_not_awaited()
    assert "do-not-log" not in caplog.text


@pytest.mark.asyncio
async def test_resource_limit_rejects_too_many_downloads_before_io(channel):
    channel._attachment_policy = replace(channel._attachment_policy, max_files=1)
    channel._download_file = AsyncMock()
    with pytest.raises(AttachmentError) as error:
        await channel._process_files(
            {"files": [{"url": "https://files.example/a"}] * 2}
        )
    assert error.value.code == "ATTACHMENT_TOO_MANY_FILES"
    channel._download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_chat_url_attachment_reaches_agent_with_local_path(channel):
    channel._download_file = AsyncMock(return_value=b"report")
    callback = AsyncMock(return_value=True)
    channel.on_message(callback)
    await channel._handle_raw_message(
        authenticated_socket(),
        request(
            files=[
                {
                    "url": "https://files.example/report?sig=secret",
                    "name": "report.xlsx",
                }
            ]
        ),
        {},
    )
    callback.assert_awaited_once()
    files = callback.call_args.args[0].params["files"]
    assert Path(files[0]["path"]).read_bytes() == b"report"
    assert "url" not in files[0]
    channel.send_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_storage_error_returns_explicit_failure_without_agent_call(
    channel, monkeypatch
):
    from unittest.mock import Mock

    monkeypatch.setattr(
        web_connect, "save_attachment", Mock(side_effect=PermissionError("test-only"))
    )
    channel._download_file = AsyncMock(return_value=b"report")
    callback = AsyncMock()
    channel.on_message(callback)
    await channel._handle_raw_message(
        authenticated_socket(), request(files=[{"url": "https://files.example/a"}]), {}
    )
    assert channel.send_response.call_args.kwargs["code"] == "ATTACHMENT_SAVE_FAILED"
    callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_concurrency_is_bounded(channel, monkeypatch):
    active = peak = 0

    async def fake_download(url, policy):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return b"report"

    monkeypatch.setattr(web_connect, "download_attachment", fake_download)
    results = await asyncio.gather(
        *(channel._download_file("https://files.example/a") for _ in range(10))
    )
    assert results == [b"report"] * 10
    assert peak == 4
