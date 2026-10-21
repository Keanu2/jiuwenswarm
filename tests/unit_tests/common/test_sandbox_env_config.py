"""Sandbox parent-environment allowlist configuration."""

import pytest
import yaml
from pathlib import Path

from jiuwenswarm.common import config


def test_env_allowlist_reads_yaml_names(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(
        "sandbox:\n  env_allowlist:\n    - CUSTOM_TOOL_SETTING\n    - EMPTY_TOOL_SETTING\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "get_config", lambda: yaml.safe_load(path.read_text(encoding="utf-8")))
    assert config.get_sandbox_env_allowlist() == ["CUSTOM_TOOL_SETTING", "EMPTY_TOOL_SETTING"]


def test_env_allowlist_default_and_explicit_empty():
    defaults = config.get_sandbox_env_allowlist({})
    assert defaults == []
    defaults.append("CUSTOM_TOOL")
    assert config.get_sandbox_env_allowlist({}) == []
    assert config.get_sandbox_env_allowlist({"sandbox": {"env_allowlist": []}}) == []


@pytest.mark.parametrize("value", [None, "PATH", ["*"], ["CLAW_*"], [1], ["A=B"], [""]])
def test_env_allowlist_rejects_invalid_names(value):
    with pytest.raises(ValueError, match="sandbox.env_allowlist"):
        config.get_sandbox_env_allowlist({"sandbox": {"env_allowlist": value}})


def test_env_allowlist_deduplicates_names():
    assert config.get_sandbox_env_allowlist({"sandbox": {"env_allowlist": ["PATH", "PATH"]}}) == ["PATH"]


def test_template_matches_default_allowlist():
    path = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources" / "config.yaml"
    template = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert config.get_sandbox_env_allowlist(template) == config.get_sandbox_env_allowlist({})


def test_path_allowlist_reads_expands_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("SHARED_DEPENDENCIES", str(tmp_path / "依赖"))
    paths = ["$SHARED_DEPENDENCIES", str(tmp_path / "依赖")]
    cfg = {"sandbox": {"path_allowlist": paths}}
    assert config.get_sandbox_path_allowlist(cfg) == [str(tmp_path / "依赖")]
    assert config.get_sandbox_path_allowlist({}) == []
    assert config.get_sandbox_path_allowlist({"sandbox": {"path_allowlist": []}}) == []


@pytest.mark.parametrize("value", [None, "C:/deps", [1], [""], ["relative"], ["*"], ["a\nb"]])
def test_path_allowlist_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="sandbox.path_allowlist"):
        config.get_sandbox_path_allowlist({"sandbox": {"path_allowlist": value}})


def test_path_allowlist_rejects_root_after_normalization(tmp_path, monkeypatch):
    root = Path(tmp_path.anchor)
    disguised_root = str(root / "dependencies" / "..")
    monkeypatch.setenv("HOST_SHARED_ROOT", disguised_root)
    for path in (str(root), disguised_root, "$HOST_SHARED_ROOT"):
        with pytest.raises(ValueError, match="sandbox.path_allowlist"):
            config.get_sandbox_path_allowlist({"sandbox": {"path_allowlist": [path]}})


@pytest.mark.parametrize("field,values,template_values", [
    ("path_allowlist", [], ["D:/new-template-default"]),
    ("path_allowlist", ["C:/shared/dependencies"], []),
    ("env_allowlist", [], ["NEW_TEMPLATE_SETTING"]),
    ("env_allowlist", ["CUSTOM_TOOL", "EMPTY_TOOL"], []),
])
def test_host_allowlist_survives_template_replacement(field, values, template_values):
    from jiuwenswarm.common.config_split import extract_user_keep_from_legacy

    old = {"sandbox": {field: values}}
    template = {"sandbox": {field: template_values}}
    kept = extract_user_keep_from_legacy(old, template)
    assert kept["sandbox"][field] == values
