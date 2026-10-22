from __future__ import annotations

import base64
from pathlib import Path

from jiuwenswarm.gateway.media_attachments import normalize_chat_media_attachments


def test_browser_image_is_persisted_under_file_sandbox(monkeypatch, tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    monkeypatch.setenv("JIUWENSWARM_FILE_SANDBOX_ROOT", str(sandbox))
    payload = {
        "media_items": [
            {
                "type": "image",
                "filename": "capture.png",
                "mimeType": "image/png",
                "base64Data": base64.b64encode(b"fake-png").decode("ascii"),
            }
        ]
    }

    normalize_chat_media_attachments(payload, "web-session")

    stored = Path(payload["media_items"][0]["path"])
    assert stored.is_relative_to(sandbox.resolve())
    assert stored.read_bytes() == b"fake-png"
    assert payload["files"]["uploaded_images"][0]["path"] == str(stored)
