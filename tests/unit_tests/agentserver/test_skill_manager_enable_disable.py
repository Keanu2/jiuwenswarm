from __future__ import annotations

import json

from jiuwenswarm.server.runtime.skill.skilldev.state_utils import (
    get_registered_skill_names,
    get_skill_enabled,
    list_disabled_skills,
    list_execution_disabled_skills,
    normalize_local_skills,
    normalize_skill_configs,
    remove_skill_config,
    set_skill_enabled,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


def test_skill_manager_default_initialization_uses_global_state_file(monkeypatch, tmp_path):
    """Default SkillManager initialization should resolve the global state file."""
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )

    manager = SkillManager()
    manager.set_skill_enabled("global-state-skill", False)
    state = json.loads((skills_dir / "skills_state.json").read_text(encoding="utf-8"))

    assert get_skill_enabled(state, "global-state-skill") is False
    assert skills_dir.is_dir()


def test_normalize_skill_configs_defaults_enabled_true():
    normalized = normalize_skill_configs(
        {
            "plugin-skill": {},
            "local-skill": {"enabled": False},
            " ": {"enabled": False},
            123: {"enabled": False},
        }
    )

    assert normalized == {
        "plugin-skill": {"enabled": True},
        "local-skill": {"enabled": False},
    }


def test_normalize_skill_configs_treats_missing_enabled_as_true():
    normalized = normalize_skill_configs(
        {
            "builtin-candidate": {"note": "no enabled field"},
        }
    )

    assert normalized["builtin-candidate"]["enabled"] is True


def test_registered_skill_names_covers_installed_plugins_and_local_skills():
    state = {
        "installed_plugins": [
            {"name": "builtin-skill"},
            {"name": "market-skill"},
        ],
        "local_skills": [
            {"name": "imported-skill"},
        ],
    }

    assert get_registered_skill_names(state) == {
        "builtin-skill",
        "market-skill",
        "imported-skill",
    }


def test_normalize_local_skills_drops_stale_records():
    local_skills = [
        {"name": "kept-skill", "origin": "C:\\keep", "source": "local"},
        {"name": "stale-skill", "origin": "C:\\stale", "source": "local"},
        {"name": "", "origin": "C:\\bad", "source": "local"},
    ]

    normalized = normalize_local_skills(local_skills, {"kept-skill"})

    assert normalized == [
        {"name": "kept-skill", "origin": "C:\\keep", "source": "local"},
    ]


def test_set_skill_enabled_supports_plugin_and_local_skill_records():
    state = {
        "installed_plugins": [{"name": "builtin-skill"}],
        "local_skills": [{"name": "imported-skill"}],
    }

    set_skill_enabled(state, "builtin-skill", False)
    set_skill_enabled(state, "imported-skill", False)

    assert get_skill_enabled(state, "builtin-skill") is False
    assert get_skill_enabled(state, "imported-skill") is False
    assert list_disabled_skills(state) == ["builtin-skill", "imported-skill"]


def test_set_skill_enabled_also_supports_uninstalled_skill():
    state = {
        "installed_plugins": [],
        "local_skills": [],
    }

    set_skill_enabled(state, "builtin-candidate", False)

    assert get_skill_enabled(state, "builtin-candidate") is False
    assert list_disabled_skills(state) == ["builtin-candidate"]
    assert list_execution_disabled_skills(state) == []


def test_get_skill_enabled_defaults_true_for_legacy_state():
    legacy_state = {
        "installed_plugins": [{"name": "legacy-plugin"}],
        "local_skills": [{"name": "legacy-local"}],
    }

    assert get_skill_enabled(legacy_state, "legacy-plugin") is True
    assert get_skill_enabled(legacy_state, "legacy-local") is True


def _make_skill_dir(skills_dir, name, body="# skill\n"):
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")
    return skill_dir


def _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir):
    skills_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_manager.get_builtin_skills_dir",
        lambda: builtin_dir,
    )
    return SkillManager()


def test_manual_skill_auto_registered_as_local(monkeypatch, tmp_path):
    """A skill folder copied in by hand becomes a local_skills record on init."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _make_skill_dir(skills_dir, "manual-skill")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)

    local = manager.get_local_skills()
    assert any(s.get("name") == "manual-skill" and s.get("source") == "local" for s in local)

    # And once registered, disabling it actually takes effect at runtime.
    manager.set_skill_enabled("manual-skill", False)
    assert "manual-skill" in manager.list_execution_disabled_skills()


def test_builtin_skill_not_auto_registered_as_local(monkeypatch, tmp_path):
    """A skill that also exists under the builtin dir must NOT be auto-registered."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(skills_dir, "builtin-twin")
    _make_skill_dir(builtin_dir, "builtin-twin")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)

    assert all(s.get("name") != "builtin-twin" for s in manager.get_local_skills())


def test_installed_builtin_skill_keeps_builtin_source_flag(monkeypatch, tmp_path):
    """Installed built-in skills are local files but still need a built-in source marker."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(skills_dir, "builtin-installed")
    _make_skill_dir(builtin_dir, "builtin-installed")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)

    listed = manager._scan_local_skills()
    skill = next(s for s in listed if s.get("name") == "builtin-installed")
    assert skill["is_builtin_source"] is True


def test_builtin_scan_uses_directory_name_without_frontmatter(monkeypatch, tmp_path):
    """Built-in marketplace entries should not appear as the generic SKILL name."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    _make_skill_dir(builtin_dir, "builtin-no-frontmatter")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)

    listed = manager._scan_builtin_skills()
    assert [s.get("name") for s in listed] == ["builtin-no-frontmatter"]


def test_already_registered_skill_not_duplicated(monkeypatch, tmp_path):
    """A skill present in installed_plugins must not get a second local record."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _make_skill_dir(skills_dir, "market-skill")
    # Pre-seed the state file so the skill is already registered as a plugin.
    (skills_dir / "skills_state.json").write_text(
        json.dumps(
            {
                "marketplaces": [],
                "installed_plugins": [{"name": "market-skill", "marketplace": "anthropic"}],
                "local_skills": [],
            }
        ),
        encoding="utf-8",
    )

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)

    assert all(s.get("name") != "market-skill" for s in manager.get_local_skills())


def test_remove_skill_config_drops_record():
    state = {"skill_configs": {"gone": {"enabled": False}, "stay": {"enabled": False}}}

    assert remove_skill_config(state, "gone") is True
    assert "gone" not in state["skill_configs"]
    assert "stay" in state["skill_configs"]
    # Removing a name that has no config is a no-op.
    assert remove_skill_config(state, "missing") is False


def test_uninstall_clears_disabled_config_so_reinstall_starts_enabled(monkeypatch, tmp_path):
    """Uninstalling a disabled skill must not leave a stale enabled=false behind."""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _make_skill_dir(skills_dir, "demo-skill")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)
    manager.set_skill_enabled("demo-skill", False)
    assert manager.get_skill_enabled("demo-skill") is False

    manager.remove_skill_config("demo-skill")

    # No residual config → a freshly reinstalled skill of the same name defaults to enabled.
    assert "demo-skill" not in manager.list_disabled_skills()
    assert manager.get_skill_enabled("demo-skill") is True


def _make_index_skill_dir(skills_dir, dir_name, frontmatter_name):
    """Create a skill whose SKILL.md frontmatter name differs from its dir name."""
    body = f"---\nname: {frontmatter_name}\ndescription: index skill\n---\n\n# index\n"
    return _make_skill_dir(skills_dir, dir_name, body)


def test_toggle_mismatched_frontmatter_name_writes_dir_name_key(monkeypatch, tmp_path):
    """frontmatter 名 ≠ 目录名的技能：禁用必须以目录名为键，执行面过滤才能命中。"""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _make_index_skill_dir(skills_dir, "health-win", "health-cli-index")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)
    manager.set_skill_enabled("health-cli-index", False)

    state = json.loads((skills_dir / "skills_state.json").read_text(encoding="utf-8"))
    assert state["skill_configs"] == {"health-win": {"enabled": False}}
    # 执行面名单输出目录名 —— SkillUseRail 的 skill.name 即目录名，过滤得以命中
    assert manager.list_execution_disabled_skills() == ["health-win"]
    # 管理面按 frontmatter 名读到的也是禁用（UI 开关状态正确）
    assert manager.get_skill_enabled("health-cli-index") is False
    assert manager.get_skill_enabled("health-win") is False


def test_legacy_frontmatter_key_effective_immediately_and_cleaned_on_write(monkeypatch, tmp_path):
    """存量 frontmatter 名旧键：不重新 toggle 也应对执行面生效，且下次写入时被清理。"""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _make_index_skill_dir(skills_dir, "health-win", "health-cli-index")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)
    # 模拟旧版本写入的 frontmatter 名键（绕过修复后的 set_skill_enabled）
    manager._state.setdefault("skill_configs", {})["health-cli-index"] = {"enabled": False}
    manager._save_state()

    # 读取回退旧键（UI 仍显示关闭）
    assert manager.get_skill_enabled("health-cli-index") is False
    # 执行面名单归一到目录名 —— 不重新 toggle 也立即生效
    assert manager.list_execution_disabled_skills() == ["health-win"]

    # 重新 toggle：写入目录名键并清掉旧键
    manager.set_skill_enabled("health-cli-index", False)
    state = json.loads((skills_dir / "skills_state.json").read_text(encoding="utf-8"))
    assert state["skill_configs"] == {"health-win": {"enabled": False}}


def test_remove_skill_config_clears_both_key_forms(monkeypatch, tmp_path):
    """卸载清理需同时移除目录名键与 frontmatter 名旧键，重装后默认启用。"""
    skills_dir = tmp_path / "skills"
    builtin_dir = tmp_path / "builtin"
    builtin_dir.mkdir(parents=True, exist_ok=True)
    _make_index_skill_dir(skills_dir, "health-win", "health-cli-index")

    manager = _init_manager_with_skills_dir(monkeypatch, skills_dir, builtin_dir)
    manager._state.setdefault("skill_configs", {})["health-cli-index"] = {"enabled": False}
    manager.set_skill_enabled("health-cli-index", True)  # 触发迁移：health-win 键 + 旧键清理
    manager.set_skill_enabled("health-cli-index", False)

    manager.remove_skill_config("health-cli-index")

    assert "health-win" not in manager.list_disabled_skills()
    assert "health-cli-index" not in manager.list_disabled_skills()
    assert manager.get_skill_enabled("health-cli-index") is True


def test_load_execution_disabled_skills_accepts_dir_name_key_for_local_skill(monkeypatch, tmp_path):
    """本地技能只登记 frontmatter 名时，目录名配置键也不应被注册表门禁误丢。"""
    from jiuwenswarm.server.runtime.skill.skilldev.state_utils import (
        load_execution_disabled_skills,
    )

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True)
    _make_index_skill_dir(skills_dir, "health-win", "health-cli-index")
    (skills_dir / "skills_state.json").write_text(
        json.dumps(
            {
                "marketplaces": [],
                "installed_plugins": [],
                "local_skills": [{"name": "health-cli-index", "origin": "local", "source": "local"}],
                "skill_configs": {"health-win": {"enabled": False}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skilldev.state_utils.get_agent_skills_dir",
        lambda: skills_dir,
    )

    assert load_execution_disabled_skills() == ["health-win"]
