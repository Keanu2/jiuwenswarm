# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Native Linux UDS authentication checks, without a deployed server or bwrap.

The daemon runs only against a temporary directory. These tests verify real
FD inheritance and same-UID rejection; namespace/Landlock deployment E2E is
a separate acceptance step.
"""

import contextlib
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile

import pytest

from jiuwenbox.supervisor import sandbox_daemon
from jiuwenbox.supervisor.daemon_ipc import CONTROL_TOKEN_BYTES, recv_frame, send_frame


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="requires native Linux UDS/pass_fds")


def rpc(path, token, header, body=b""):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        if token is not None:
            send_frame(sock, token)
        with contextlib.suppress(OSError):
            send_frame(sock, json.dumps(header).encode())
            if body:
                sock.sendall(body)
        try:
            response = json.loads(recv_frame(sock, 65536))
        except ConnectionError:
            return None, b""  # Early rejection may reset a peer still sending data.
        data = b""
        if response.get("ok") and response.get("content_size"):
            data = recv_frame(sock, 65536)
        return response, data


@pytest.fixture
def native_daemon():
    # Keep the UDS pathname under the kernel's short sockaddr_un limit.
    with tempfile.TemporaryDirectory(prefix="jbx-ipc-") as directory:
        root = Path(directory)
        path = root / "control.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(8)
        token = os.urandom(CONTROL_TOKEN_BYTES)
        env = dict(os.environ)
        env[sandbox_daemon.LISTENER_FD_ENV] = str(listener.fileno())
        process = subprocess.Popen(
            [sys.executable, "-S", sandbox_daemon.__file__],
            pass_fds=(listener.fileno(),), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        listener.close()
        try:
            process.stdin.write(struct.pack(">I", len(token)) + token)
            process.stdin.close()
            process.stdin = None
            # Wait for the real accept loop by sending an authenticated harmless request.
            response, _ = rpc(path, token, {"type": "probe"})
            assert response["error"] == "unknown_request_type"
            yield process, root, path, token
        finally:
            if process.poll() is None:
                with contextlib.suppress(OSError, ValueError):
                    rpc(path, token, {"type": "shutdown"})
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            process.communicate(timeout=5)


def test_same_uid_legacy_client_cannot_execute_or_shutdown(native_daemon):
    process, root, path, _ = native_daemon
    marker = root / "unauthorized-marker"
    code = """
import json, os, socket, struct, sys
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
s.settimeout(5)
s.connect(sys.argv[1])
command=[sys.executable,'-c','from pathlib import Path; Path('+repr(sys.argv[2])+').write_text("unauthorized")']
payload=json.dumps({'v':1,'type':'exec','command':command,'stdin_size':0}).encode()
def exact(n):
    data=b''
    while len(data)<n:
        chunk=s.recv(n-len(data))
        if not chunk: raise ConnectionError('unexpected EOF')
        data+=chunk
    return data
try:
    s.sendall(struct.pack('>I',len(payload))+payload)
    size=struct.unpack('>I',exact(4))[0]
    response=json.loads(exact(size))
    error=response.get('error')
except OSError:
    error='connection_closed'
print(json.dumps({'uid':os.getuid(),'error':error}))
s.close()
"""
    attacker = subprocess.run(
        [sys.executable, "-c", code, str(path), str(marker)], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10, check=True,
    )
    result = json.loads(attacker.stdout)
    assert result["uid"] == os.getuid()
    assert result["error"] in ("unauthorized", "connection_closed")
    assert not marker.exists()
    response, _ = rpc(path, None, {"type": "shutdown"})
    assert response is None or response["error"] == "unauthorized"
    assert process.poll() is None


def test_authenticated_exec_and_file_operations(native_daemon):
    process, root, path, token = native_daemon
    marker = root / "authorized-file"
    response, _ = rpc(path, token, {
        "type": "write_file", "path": str(marker), "content_size": 7,
    }, b"payload")
    assert response["ok"] is True
    response, body = rpc(path, token, {"type": "read_file", "path": str(marker)})
    assert response["ok"] is True and body == b"payload"
    response, _ = rpc(path, token, {
        "type": "exec", "command": [sys.executable, "-c", "print('authorized')"], "stdin_size": 0,
    })
    assert response["exit_code"] == 0
    assert response["stdout"].strip() == "authorized"
    for filename in ("cmdline", "environ"):
        if token in Path(f"/proc/{process.pid}/{filename}").read_bytes():
            pytest.fail("Control token leaked into process metadata")


def test_wrong_token_cannot_write_file(native_daemon):
    _, root, path, token = native_daemon
    marker = root / "wrong-token-file"
    wrong = bytes(value ^ 0xFF for value in token)
    response, _ = rpc(path, wrong, {
        "type": "write_file", "path": str(marker), "content_size": 7,
    }, b"payload")
    assert response is None or response["error"] == "unauthorized"
    assert not marker.exists()


def test_runtime_listener_tokens_are_independent(monkeypatch):
    from jiuwenbox.server.runtime.process import ProcessRuntime

    with tempfile.TemporaryDirectory(prefix="jbx-ipc-") as directory:
        runtime = ProcessRuntime()
        monkeypatch.setattr(runtime, "_sandbox_root", lambda: Path(directory))
        with runtime._create_daemon_listener("first", None), runtime._create_daemon_listener("second", None):
            first = runtime._daemon_control_tokens[runtime._control_socket_host_path("first")]
            second = runtime._daemon_control_tokens[runtime._control_socket_host_path("second")]
            assert len(first) == len(second) == CONTROL_TOKEN_BYTES
            assert first != second
