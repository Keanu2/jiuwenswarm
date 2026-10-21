# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the web 任务接续 toggle's persistence layer.

The toggle writes ``tasks.enabled`` through :func:`set_tasks_enabled` (the
``update_config`` transaction) and the AgentServer re-resolves the section via
``get_config()`` afterwards. What these tests pin:

* the write lands in config.yaml without disturbing neighbouring keys/comments
  (round-trip through ruamel, not a ``safe_load``/``safe_dump`` rebuild);
* ``resolve_tasks_config`` observes the new value immediately — the parse cache
  is keyed on the file stamp, so the write invalidates it;
* a missing ``tasks`` section is created rather than raising.
"""

from __future__ import annotations

import pytest

import jiuwenswarm.common.config as config_mod
from jiuwenswarm.common.config import set_tasks_enabled
from jiuwenswarm.agents.harness.team.tasks import resolve_tasks_config


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    """Point every config helper at a temporary config.yaml."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "# 部署配置\n"
        "model_name: test-model\n"
        "tasks:\n"
        "  base_url: http://relay.test:8000\n"
        "  api_key: a2x_pat_test\n"
        "  enabled: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", config_file)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: config_file)
    monkeypatch.delenv("TASKS_ENABLED", raising=False)
    monkeypatch.delenv("TASKS_BASE_URL", raising=False)
    monkeypatch.delenv("TASKS_API_KEY", raising=False)
    return config_file


def test_set_tasks_enabled_flips_the_switch_in_place(tmp_config):
    """Toggling on rewrites only tasks.enabled; comments and siblings survive."""
    set_tasks_enabled(True)

    text = tmp_config.read_text(encoding="utf-8")
    assert "# 部署配置" in text
    assert "model_name: test-model" in text
    assert "enabled: true" in text

    config = resolve_tasks_config()
    assert config.enabled is True
    assert config.base_url == "http://relay.test:8000"
    assert config.api_key == "a2x_pat_test"


def test_set_tasks_enabled_off_round_trip(tmp_config):
    set_tasks_enabled(True)
    set_tasks_enabled(False)

    config = resolve_tasks_config()
    assert config.enabled is False


def test_set_tasks_enabled_creates_missing_section(tmp_path, monkeypatch):
    """A config without a tasks section gains one instead of raising."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text("model_name: test-model\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_YAML_PATH", config_file)
    monkeypatch.setattr(config_mod, "get_config_file", lambda: config_file)
    monkeypatch.delenv("TASKS_ENABLED", raising=False)

    set_tasks_enabled(True)

    config = resolve_tasks_config()
    assert config.enabled is True


def test_service_ready_reflects_api_key_presence(tmp_config):
    """The UI hint's service_ready: base_url alone is not enough.

    ``resolve_tasks_config`` reports ``configured`` as enabled + base_url, but
    the anonymous mode is a configuration error (the relay identifies the
    device by its key), so the toggle handler derives service readiness from
    base_url *and* api_key. Pin that derivation here.
    """
    config = resolve_tasks_config()
    assert bool(config.base_url and config.api_key) is True

    tmp_config.write_text(
        "tasks:\n"
        "  base_url: http://relay.test:8000\n"
        "  enabled: true\n",
        encoding="utf-8",
    )
    config = resolve_tasks_config()
    assert config.enabled is True
    assert bool(config.base_url and config.api_key) is False
