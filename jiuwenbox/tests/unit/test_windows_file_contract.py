# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Windows file IPC errors must reach the existing fixed HTTP 404 responses."""

import asyncio
import errno
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
from jiuwenbox.server import app as app_module
from jiuwenbox.server.limits import SandboxLimits
from jiuwenbox.server.runtime.base import RuntimeFileOpResult
from jiuwenbox.server.runtime.process import ProcessRuntime
from jiuwenbox.server.sandbox_manager import SandboxManager
from jiuwenbox.supervisor import win_exec


@pytest.fixture
def file_client(monkeypatch):
    def invoke(handler, header):
        responses = []
        with monkeypatch.context() as patch:
            patch.setattr(win_exec, "_send_response", lambda _stream, result: responses.append(result))
            handler(None, header)
        response = responses[-1]
        if response.get("ok"):
            return RuntimeFileOpResult(ok=True, items=response.get("items"))
        return ProcessRuntime._file_op_result_from_response(response)

    class Runtime:
        async def read_file(self, _sid, path):
            return invoke(win_exec._handle_read_file_request, {"path": path})

        async def list_dir(self, _sid, path, **options):
            return invoke(win_exec._handle_list_dir_request, {"path": path, **options})

    manager = SandboxManager.__new__(SandboxManager)
    manager._lock = asyncio.Lock()
    manager._sandboxes = {"sb": SandboxRef(id="sb", phase=SandboxPhase.READY)}
    manager.policy = SecurityPolicy()
    manager._policies = {"sb": manager.policy}
    manager.runtime = Runtime()
    manager.audit = SimpleNamespace(log=lambda *args, **kwargs: None)
    manager.limits = SandboxLimits()
    monkeypatch.delenv("JIUWENBOX_API_TOKEN", raising=False)
    monkeypatch.setattr(app_module, "_sandbox_manager", manager)
    return TestClient(app_module.create_app())


@pytest.mark.parametrize("endpoint,options,message", [
    ("download", {}, "File not found"),
    ("files", {}, "Directory not found"),
    ("files", {"recursive": "true"}, "Directory not found"),
])
def test_missing_windows_path_has_fixed_404(file_client, tmp_path, endpoint, options, message):
    missing = str(tmp_path / "private-path-must-not-be-echoed")
    response = file_client.get(f"/api/v1/sandboxes/sb/{endpoint}",
                               params={"sandbox_path": missing, **options})
    assert response.status_code == 404
    assert response.json() == {"error": message}
    assert missing not in response.text


@pytest.mark.parametrize("exc,code", [
    (FileNotFoundError(errno.ENOENT, "missing", "private-file"), "not_found"),
    (NotADirectoryError(errno.ENOTDIR, "not a directory", "private-file"), "not_a_directory"),
    (IsADirectoryError(errno.EISDIR, "is a directory", "private-file"), "is_directory"),
    (PermissionError(errno.EACCES, "denied", "private-file"), "permission_denied"),
])
def test_file_error_codes_do_not_include_filename(exc, code):
    response = win_exec._file_error_response(exc)
    assert response["error"] == code
    assert "private-file" not in response["stderr"]


def test_file_used_as_listing_root_is_not_reported_missing(file_client, tmp_path):
    target = tmp_path / "file.txt"
    target.write_text("payload", encoding="utf-8")
    response = file_client.get("/api/v1/sandboxes/sb/files", params={"sandbox_path": str(target)})
    assert response.status_code == 409
    assert "not a directory" in response.json()["error"]
