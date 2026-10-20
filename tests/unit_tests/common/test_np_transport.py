# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""np_transport 单测：帧编解码（与 claw_desktop length-prefix.ts 同规范）+
Windows 命名管道 roundtrip / 对端进程身份校验 / httpx 管道 transport。"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from jiuwenswarm.common.np_transport import (
    FRAME_MAX_BYTES,
    FrameCodecError,
    FrameDecoder,
    encode_frame,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="命名管道仅 Windows")


class TestFrameCodec:
    """帧编解码纯逻辑（不依赖管道）。"""

    def test_roundtrip(self) -> None:
        payload = {"type": "auth", "token": "abc", "嵌套": {"n": 1}}
        data = encode_frame(payload)
        assert int.from_bytes(data[:4], "little") == len(data) - 4
        assert FrameDecoder().feed(data) == [payload]

    def test_sticky_packets(self) -> None:
        frames = [{"id": 1}, {"id": 2, "text": "你好"}, ["array-frame"]]
        blob = b"".join(encode_frame(f) for f in frames)
        assert FrameDecoder().feed(blob) == frames

    def test_half_packet_byte_by_byte(self) -> None:
        payload = {"msg": "逐字节", "data": [1, 2, 3]}
        blob = encode_frame(payload)
        dec = FrameDecoder()
        out: list = []
        for i in range(len(blob)):
            out.extend(dec.feed(blob[i : i + 1]))
        assert out == [payload]
        assert dec.pending_bytes == 0

    def test_split_across_frame_boundary(self) -> None:
        a, b = {"x": "a" * 100}, {"x": "b"}
        blob = encode_frame(a) + encode_frame(b)
        dec = FrameDecoder()
        cut = len(blob) - 2
        assert dec.feed(blob[:cut]) == [a]
        assert dec.feed(blob[cut:]) == [b]

    def test_oversize_rejected_on_encode(self) -> None:
        with pytest.raises(FrameCodecError):
            encode_frame({"data": "x" * FRAME_MAX_BYTES})

    def test_oversize_rejected_on_decode(self) -> None:
        dec = FrameDecoder(max_bytes=1024)
        with pytest.raises(FrameCodecError, match="超长"):
            dec.feed((2048).to_bytes(4, "little"))

    def test_zero_length_rejected(self) -> None:
        with pytest.raises(FrameCodecError, match="长度为 0"):
            FrameDecoder().feed((0).to_bytes(4, "little"))

    def test_invalid_json_rejected(self) -> None:
        body = b"not-json"
        with pytest.raises(FrameCodecError, match="非合法 JSON"):
            FrameDecoder().feed(len(body).to_bytes(4, "little") + body)

    def test_max_boundary_ok(self) -> None:
        payload = {"data": "x" * (FRAME_MAX_BYTES - 64)}
        assert FrameDecoder().feed(encode_frame(payload)) == [payload]


class TestImageVerifier:
    """镜像白名单兼容 uv 链接，但不能随对端路径的链接变化放行。"""

    @pytest.mark.parametrize("use_resolved_image", [False, True])
    def test_accepts_allowlisted_uv_link_and_target(self, monkeypatch, use_resolved_image) -> None:
        from jiuwenswarm.common import np_transport

        link = r"C:\uv\cpython-3.13-windows-x86_64-none\python.exe"
        target = r"C:\uv\cpython-3.13.13-windows-x86_64-none\python.exe"
        realpath = os.path.realpath
        monkeypatch.setattr(
            np_transport.os.path, "realpath", lambda p: target if p == link else realpath(p)
        )
        monkeypatch.setattr(
            np_transport.win32process,
            "GetModuleFileNameEx",
            lambda _handle, _module: target if use_resolved_image else link,
        )

        assert np_transport.make_image_verifier([link])(os.getpid()) is True

    def test_rejects_unlisted_image_after_directory_retarget(self, monkeypatch) -> None:
        from jiuwenswarm.common import np_transport

        trusted = r"C:\trusted\python.exe"
        image = r"C:\untrusted\python.exe"
        targets = {image: image}
        realpath = os.path.realpath
        monkeypatch.setattr(
            np_transport.os.path, "realpath", lambda p: targets[p] if p in targets else realpath(p)
        )
        monkeypatch.setattr(
            np_transport.win32process, "GetModuleFileNameEx", lambda _handle, _module: image
        )
        verify = np_transport.make_image_verifier([trusted])
        assert verify(os.getpid()) is False

        # 进程启动后原目录被改名，并在原位置创建指向可信目录的 Junction。
        # 已加载的镜像不变，即使当前磁盘路径解析到白名单，也必须继续拒绝。
        targets[image] = trusted
        assert verify(os.getpid()) is False


class TestPipeRoundtrip:
    """真实命名管道 roundtrip（本机）。"""

    @pytest.mark.asyncio
    async def test_echo_server_client(self) -> None:
        from jiuwenswarm.common.np_transport import open_pipe, serve_pipe

        path = rf"\\.\pipe\claw-test-np-{os.getpid()}-echo"

        async def echo(stream) -> None:
            while True:
                try:
                    frame = await stream.recv_frame()
                except Exception:
                    return
                await stream.send_frame({"echo": frame})

        server = await serve_pipe(path, echo)
        try:
            client = await open_pipe(path, timeout=5)
            await client.send_frame({"hello": "世界"})
            assert await client.recv_frame() == {"echo": {"hello": "世界"}}
            await client.send_frame({"n": 2})
            assert await client.recv_frame() == {"echo": {"n": 2}}
            await client.close()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_verify_client_rejects(self) -> None:
        from jiuwenswarm.common.np_transport import open_pipe, serve_pipe

        path = rf"\\.\pipe\claw-test-np-{os.getpid()}-verify"

        async def handler(stream) -> None:  # pragma: no cover - 不该被触达
            await stream.send_frame({"should": "not reach"})

        server = await serve_pipe(path, handler, verify_client=lambda pid: False)
        try:
            client = await open_pipe(path, timeout=5)
            # 对端校验失败 → 服务端立即断开：写可能成功（缓冲），读必为空（对端已关）
            with pytest.raises(Exception):
                await client.send_frame({"x": 1})
                    # 断管后读应报关闭
                await client.recv_frame(timeout=2)
            await client.close()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_verify_client_accepts_self_image(self) -> None:
        import win32api
        import win32con
        import win32process

        from jiuwenswarm.common.np_transport import (
            make_image_verifier,
            open_pipe,
            serve_pipe,
        )

        # 白名单 = 本进程真实镜像（注意 venv launcher ≠ 真实 python.exe，须用同一 API 取）
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, os.getpid()
        )
        try:
            real_image = win32process.GetModuleFileNameEx(handle, 0)
        finally:
            win32api.CloseHandle(handle)

        verifier = make_image_verifier([real_image])
        assert verifier(os.getpid()) is True
        assert make_image_verifier([r"C:\nonexistent\fake.exe"])(os.getpid()) is False

        path = rf"\\.\pipe\claw-test-np-{os.getpid()}-img"

        async def handler(stream) -> None:
            frame = await stream.recv_frame()
            await stream.send_frame({"ok": True, "got": frame})

        server = await serve_pipe(path, handler, verify_client=verifier)
        try:
            client = await open_pipe(path, timeout=5)
            await client.send_frame({"ping": 1})
            assert (await client.recv_frame())["ok"] is True
            await client.close()
        finally:
            await server.stop()


def _real_self_image() -> str:
    """当前进程真实镜像（venv launcher ≠ 镜像，须走 Win32 API 取）。"""
    import win32api
    import win32con
    import win32process

    handle = win32api.OpenProcess(
        win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, os.getpid()
    )
    try:
        return win32process.GetModuleFileNameEx(handle, 0)
    finally:
        win32api.CloseHandle(handle)


class TestPipeServerVerification:
    """出站客户端的服务端身份校验（2026-10-17，跨 SID 预创建抢占防护 P0）。"""

    @pytest.fixture(autouse=True)
    def _reset_verifier_state(self, monkeypatch):
        from jiuwenswarm.common import np_transport
        import jiuwenswarm.common.secrets_bootstrap as secrets_mod

        monkeypatch.setattr(np_transport, "_default_server_verifier", None)
        monkeypatch.setattr(np_transport, "_configured_server_images", None)
        monkeypatch.setattr(secrets_mod, "_SECRETS", {})
        monkeypatch.setattr(secrets_mod, "_LOADED", False)
        yield

    @pytest.mark.asyncio
    async def test_server_images_accept_self(self) -> None:
        from jiuwenswarm.common.np_transport import open_pipe, serve_pipe

        path = rf"\\.\pipe\claw-test-np-{os.getpid()}-srv-ok"

        async def handler(stream) -> None:
            frame = await stream.recv_frame()
            await stream.send_frame({"ok": True, "got": frame})

        server = await serve_pipe(path, handler)
        try:
            client = await open_pipe(path, timeout=5, server_images={_real_self_image()})
            await client.send_frame({"ping": 1})
            assert (await client.recv_frame())["ok"] is True
            await client.close()
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_server_images_reject_foreign_no_bytes_sent(self) -> None:
        """白名单不含服务端镜像 → fail-closed：连接被拒且服务端收不到任何字节。"""
        from jiuwenswarm.common.np_transport import PipeError, open_pipe, serve_pipe

        path = rf"\\.\pipe\claw-test-np-{os.getpid()}-srv-reject"
        received: list = []

        async def handler(stream) -> None:
            # 抢占者视角：记录任何到达的字节（应永远为空）
            try:
                chunk = await stream.read(timeout=2)
                received.append(chunk)
            except Exception:
                pass

        server = await serve_pipe(path, handler)
        try:
            with pytest.raises(PipeError, match="身份校验"):
                await open_pipe(path, timeout=5, server_images={r"C:\nonexistent\evil.exe"})
            await asyncio.sleep(0.2)
            # 客户端校验失败即断管：服务端最多读到空字节（EOF），绝无业务数据
            assert [b for b in received if b] == []
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_default_verifier_uses_desktop_exe_secret(self, monkeypatch) -> None:
        """默认白名单 = 密钥包 desktopExe；无密钥包时不校验（兼容旧形态）。"""
        import jiuwenswarm.common.secrets_bootstrap as secrets_mod
        from jiuwenswarm.common.np_transport import PipeError, open_pipe, serve_pipe

        async def handler(stream) -> None:
            frame = await stream.recv_frame()
            await stream.send_frame({"ok": True})

        # 1) 无密钥包：不校验，直接连通
        path1 = rf"\\.\pipe\claw-test-np-{os.getpid()}-srv-nodef"
        server1 = await serve_pipe(path1, handler)
        try:
            client = await open_pipe(path1, timeout=5)
            await client.send_frame({"x": 1})
            assert (await client.recv_frame())["ok"] is True
            await client.close()
        finally:
            await server1.stop()

        # 2) desktopExe = 本进程镜像：默认校验通过
        monkeypatch.setattr(
            secrets_mod, "_SECRETS", {"desktopExe": _real_self_image()}
        )
        path2 = rf"\\.\pipe\claw-test-np-{os.getpid()}-srv-defok"
        server2 = await serve_pipe(path2, handler)
        try:
            client = await open_pipe(path2, timeout=5)
            await client.send_frame({"x": 2})
            assert (await client.recv_frame())["ok"] is True
            await client.close()
        finally:
            await server2.stop()

    @pytest.mark.asyncio
    async def test_default_verifier_rejects_when_desktop_exe_mismatch(self, monkeypatch) -> None:
        import jiuwenswarm.common.secrets_bootstrap as secrets_mod
        from jiuwenswarm.common.np_transport import PipeError, open_pipe, serve_pipe

        monkeypatch.setattr(
            secrets_mod, "_SECRETS", {"desktopExe": r"C:\nonexistent\evil-desktop.exe"}
        )
        path = rf"\\.\pipe\claw-test-np-{os.getpid()}-srv-defreject"
        received: list = []

        async def handler(stream) -> None:
            try:
                chunk = await stream.read(timeout=2)
                received.append(chunk)
            except Exception:
                pass

        server = await serve_pipe(path, handler)
        try:
            with pytest.raises(PipeError, match="身份校验"):
                await open_pipe(path, timeout=5)
            await asyncio.sleep(0.2)
            assert [b for b in received if b] == []
        finally:
            await server.stop()


class TestNamedPipeHttpTransport:
    """HTTP/1.1 over 命名管道（对端用 serve_pipe 模拟桌面代理）。"""

    async def _start_http_server(self, responder):
        from jiuwenswarm.common.np_transport import serve_pipe

        path = rf"\\.\pipe\claw-test-np-{os.getpid()}-{id(responder) & 0xFFFF}"

        async def handle(stream) -> None:
            # 极简 HTTP 服务：读到 \r\n\r\n + Content-Length 体
            buf = bytearray()
            while b"\r\n\r\n" not in buf:
                chunk = await stream.read()
                if not chunk:
                    return
                buf.extend(chunk)
            head, _, rest = bytes(buf).partition(b"\r\n\r\n")
            headers = {}
            for line in head.split(b"\r\n")[1:]:
                if b":" in line:
                    k, v = line.split(b":", 1)
                    headers[k.decode().strip().lower()] = v.decode().strip()
            body = rest
            remaining = int(headers.get("content-length", "0")) - len(body)
            while remaining > 0:
                chunk = await stream.read(remaining)
                if not chunk:
                    break
                body += chunk
                remaining -= len(chunk)
            status, resp_headers, resp_body = responder(head.decode("latin-1"), body, headers)
            payload = f"HTTP/1.1 {status} OK\r\n".encode()
            for k, v in resp_headers.items():
                payload += f"{k}: {v}\r\n".encode()
            payload += b"\r\n" + resp_body
            await stream.write(payload)

        server = await serve_pipe(path, handle)
        return path, server

    @pytest.mark.asyncio
    async def test_get_content_length(self) -> None:
        import httpx

        from jiuwenswarm.common.np_transport import NamedPipeTransport

        def responder(head, body, headers):
            assert head.startswith("GET /v1/models")
            assert headers.get("authorization") == "Bearer k"
            return 200, {"Content-Type": "application/json"}, json.dumps({"ok": True}).encode()

        path, server = await self._start_http_server(responder)
        try:
            async with httpx.AsyncClient(transport=NamedPipeTransport(path)) as client:
                resp = await client.get(
                    "http://pipe.local/v1/models", headers={"Authorization": "Bearer k"}
                )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_post_chunked_sse(self) -> None:
        import httpx

        from jiuwenswarm.common.np_transport import NamedPipeTransport

        chunks = [b'data: {"a":1}\n\n', b'data: {"b":2}\n\n', b"data: [DONE]\n\n"]

        def responder(head, body, headers):
            assert head.startswith("POST /v1/chat/completions")
            assert json.loads(body.decode()) == {"model": "m1"}
            payload = b"".join(b"%x\r\n" % len(c) + c + b"\r\n" for c in chunks) + b"0\r\n\r\n"
            return 200, {"Transfer-Encoding": "chunked", "Content-Type": "text/event-stream"}, payload

        # chunked 响应由模拟服务端手工编码——responder 返回的 body 即原始 chunked 字节流
        async def chunked_responder(head, body, headers):
            s, h, raw = responder(head, body, headers)
            return s, h, raw

        path, server = await self._start_http_server(lambda h, b, hs: chunked_responder_raw(h, b, hs))
        try:
            async with httpx.AsyncClient(transport=NamedPipeTransport(path)) as client:
                resp = await client.post("http://pipe.local/v1/chat/completions", json={"model": "m1"})
            assert resp.status_code == 200
            assert resp.text == "".join(c.decode() for c in chunks)
        finally:
            await server.stop()

    @pytest.mark.asyncio
    async def test_eof_terminated_body(self) -> None:
        import httpx

        from jiuwenswarm.common.np_transport import NamedPipeTransport

        def responder(head, body, headers):
            # 无 Content-Length：Connection: close + 读到 EOF
            return 200, {"Content-Type": "text/plain"}, b"stream-body"

        path, server = await self._start_http_server(responder)
        try:
            async with httpx.AsyncClient(transport=NamedPipeTransport(path)) as client:
                resp = await client.get("http://pipe.local/x")
            assert resp.text == "stream-body"
        finally:
            await server.stop()


def chunked_responder_raw(head, body, headers):
    """chunked 响应（原始 chunked 编码字节流，由 transport 解码）。"""
    chunks = [b'data: {"a":1}\n\n', b'data: {"b":2}\n\n', b"data: [DONE]\n\n"]
    payload = b"".join(b"%x\r\n" % len(c) + c + b"\r\n" for c in chunks) + b"0\r\n\r\n"
    return 200, {"Transfer-Encoding": "chunked", "Content-Type": "text/event-stream"}, payload
