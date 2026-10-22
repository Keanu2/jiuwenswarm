from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.file_sandbox_rail import FileSandboxRail
from jiuwenswarm.common.file_sandbox import (
    FileSandboxViolation,
    copy_file_into_sandbox,
    get_file_sandbox_root,
    persist_received_file,
    persist_sandbox_bytes,
    resolve_file_sandbox_path,
)


@pytest.fixture
def sandbox(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "sandbox"
    monkeypatch.setenv("JIUWENSWARM_FILE_SANDBOX_ROOT", str(root))
    return get_file_sandbox_root()


def test_resolve_rejects_parent_escape_and_symlink_escape(sandbox: Path, tmp_path: Path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(FileSandboxViolation):
        resolve_file_sandbox_path(outside)
    with pytest.raises(FileSandboxViolation):
        resolve_file_sandbox_path("../outside.txt")

    link = sandbox / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(FileSandboxViolation):
        resolve_file_sandbox_path(link / "outside.txt")


def test_copy_and_persist_land_under_private_sandbox(sandbox: Path, tmp_path: Path):
    source = tmp_path / "source.txt"
    source.write_text("source", encoding="utf-8")

    copied = copy_file_into_sandbox(source, subdir=("inbox", "web", "s1"))
    received = persist_sandbox_bytes(
        b"received",
        filename="received.txt",
        subdir=("inbox", "a2a", "s1"),
    )

    assert copied.is_relative_to(sandbox)
    assert copied.read_text(encoding="utf-8") == "source"
    assert received.is_relative_to(sandbox)
    assert received.read_bytes() == b"received"


def test_file_sandbox_root_follows_received_files_dir(monkeypatch, tmp_path: Path):
    inbox = tmp_path / "Desktop" / "JiuwenSwarm"
    monkeypatch.delenv("JIUWENSWARM_FILE_SANDBOX_ROOT", raising=False)
    monkeypatch.setenv("JIUWENSWARM_RECEIVED_FILES_DIR", str(inbox))

    root = get_file_sandbox_root()
    assert root == inbox.resolve()
    assert resolve_file_sandbox_path("notes.txt") == inbox.resolve() / "notes.txt"


def test_persist_received_file_uses_desktop_override(monkeypatch, tmp_path: Path):
    inbox = tmp_path / "Desktop" / "JiuwenSwarm"
    monkeypatch.setenv("JIUWENSWARM_RECEIVED_FILES_DIR", str(inbox))

    saved = persist_received_file(b"hello-desktop", filename="photo.jpg")
    again = persist_received_file(b"hello-desktop-2", filename="photo.jpg")

    assert saved == inbox / "photo.jpg"
    assert saved.read_bytes() == b"hello-desktop"
    assert again == inbox / "photo (1).jpg"
    assert resolve_file_sandbox_path(saved) == saved.resolve()


def _tool_context(name: str, arguments: dict):
    inputs = SimpleNamespace(
        tool_name=name,
        tool_args=arguments,
        tool_call=SimpleNamespace(id="call-1"),
        tool_result=None,
        tool_msg=None,
    )
    return SimpleNamespace(inputs=inputs, extra={})


@pytest.mark.asyncio
async def test_file_sandbox_rail_rewrites_relative_paths_and_blocks_escape(sandbox: Path, tmp_path: Path):
    rail = FileSandboxRail()
    allowed = _tool_context("read_file", {"file_path": "notes/a.md"})
    await rail.before_tool_call(allowed)
    assert allowed.extra == {}
    assert allowed.inputs.tool_args["file_path"] == str(sandbox / "notes" / "a.md")

    denied = _tool_context("read_file", {"file_path": str(tmp_path / "outside.md")})
    await rail.before_tool_call(denied)
    assert denied.extra["_skip_tool"] is True
    assert "outside" in denied.inputs.tool_result["error"]


@pytest.mark.asyncio
async def test_file_sandbox_rail_blocks_host_shell(sandbox: Path):
    ctx = _tool_context("bash", {"command": f"cat {sandbox / 'ok.txt'}"})
    await FileSandboxRail().before_tool_call(ctx)
    assert ctx.extra["_skip_tool"] is True
    assert "disabled" in ctx.inputs.tool_result["error"]
