# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from unittest.mock import MagicMock

from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    _ensure_bash_background_card,
    _ensure_powershell_background_card,
    _patch_tool_class,
    _shell_mismatch,
    _wrap_invoke,
    _pre_execute_shell_command,
    install_shell_tool_safety_hooks,
    reset_installed_flag,
)


@pytest.fixture(autouse=True)
def _reset_install_flag():
    reset_installed_flag()
    yield
    reset_installed_flag()


def test_pre_execute_blocks_pkill_on_jiuwenswarm_tui() -> None:
    err = _pre_execute_shell_command('pkill -f "jiuwenswarm-tui" 2>/dev/null', "bash")
    assert err is not None
    assert "rejected for safety" in err


def test_pre_execute_allows_unrelated_ps() -> None:
    err = _pre_execute_shell_command("ps aux | grep node | head -5", "bash")
    assert err is None


def test_bash_rejects_powershell_cmdlet() -> None:
    err = _shell_mismatch("bash", 'Move-Item "a" "b"')
    assert err is not None
    assert "powershell" in err


def test_bash_rejects_chained_and_piped_cmdlets() -> None:
    assert _shell_mismatch("bash", 'cd "C:/x" && Move-Item -Path a -Destination b') is not None
    assert _shell_mismatch("bash", "Get-ChildItem | Move-Item a b") is not None
    assert _shell_mismatch("bash", "ls; Remove-Item b") is not None


def test_bash_rejects_powershell_specific_variables_and_herestrings() -> None:
    assert _shell_mismatch("bash", "Get-Content $env:APPDATA\\x") is not None
    assert _shell_mismatch("bash", "echo $null") is not None
    assert _shell_mismatch("bash", "@'\nraw text\n'@ | Set-Content x") is not None


def test_bash_allows_generic_bash_variables() -> None:
    # A bare $var is legal bash; only $env:/$null/$true/$false are PowerShell.
    for command in (
        "echo $HOME",
        "for f in *.txt; do echo $f; done",
        "export VAR=1 && echo $VAR",
        "grep $pattern file.txt",
        "echo $nothing",
        "ls $dir",
    ):
        assert _shell_mismatch("bash", command) is None, command


def test_bash_allows_cmdlet_name_substrings_in_filenames() -> None:
    # Cmdlet detection matches the first word of each segment, never substrings.
    for command in (
        "cat out-file.txt",
        "bash move-item.sh",
        "grep start-process run.log",
    ):
        assert _shell_mismatch("bash", command) is None, command


def test_bash_allows_posix_loops_and_plain_commands() -> None:
    for command in (
        "cd /d/xiaoyi_work && ls -la",
        "mkdir -p build && touch build/a.txt",
        "ls; echo done",
        "git log --format=%H",
    ):
        assert _shell_mismatch("bash", command) is None, command


@pytest.mark.asyncio
async def test_bash_wrapper_forces_bash_shell_type() -> None:
    calls = []

    class Parsed:
        command = "echo hello"

    class Tool:
        def _parse_inputs(self, inputs):
            return Parsed()

    async def original(self, inputs, **kwargs):
        calls.append(dict(inputs))
        return "ok"

    wrapped = _wrap_invoke(original, "bash")
    result = await wrapped(Tool(), {"command": "echo hello"})

    assert result == "ok"
    assert calls == [{"command": "echo hello", "shell_type": "bash"}]


@pytest.mark.asyncio
async def test_bash_wrapper_drops_hidden_background_inputs() -> None:
    calls = []

    class Parsed:
        command = "python -m http.server 8000"

    class Tool:
        def _parse_inputs(self, inputs):
            return Parsed()

    async def original(self, inputs, **kwargs):
        calls.append(dict(inputs))
        return "ok"

    wrapped = _wrap_invoke(original, "bash")
    result = await wrapped(
        Tool(),
        {
            "command": Parsed.command,
            "run_in_background": True,
            "background": True,
            "timeout": 1800,
        },
    )

    assert result == "ok"
    assert calls[0]["shell_type"] == "bash"
    assert calls[0]["timeout"] == 1800
    assert "run_in_background" not in calls[0]
    assert "background" not in calls[0]


def test_install_hides_bash_background_input() -> None:
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    install_shell_tool_safety_hooks()
    tool = BashTool(MagicMock(), language="cn")
    properties = tool.card.input_params["properties"]
    assert "run_in_background" not in properties
    assert "description" not in properties
    assert "shell_type" not in properties
    assert "执行 Bash 命令并返回输出" in tool.card.description
    assert "run_in_background" not in tool.card.description
    assert "起前" not in tool.card.description
    assert "子进程" not in tool.card.description
    assert "失败换端口" not in tool.card.description
    assert "不要杀占用进程" not in tool.card.description


def test_ensure_bash_background_card_sets_compact_en_description() -> None:
    class Card:
        input_params = {"properties": {}}
        description = "bash tool"

    class Tool:
        card = Card()

    _ensure_bash_background_card(Tool(), "en")
    assert "run_in_background" not in Tool.card.input_params["properties"]
    assert "shell_type" not in Tool.card.input_params["properties"]
    assert Tool.card.description.startswith("Execute a command with Bash")
    assert "will not exit on its own" not in Tool.card.description
    assert "Before start" not in Tool.card.description
    assert "child of this PID" not in Tool.card.description


def test_install_hides_powershell_background_input() -> None:
    from openjiuwen.harness.tools.shell.powershell._tool import PowerShellTool

    install_shell_tool_safety_hooks()
    tool = PowerShellTool(MagicMock(), language="cn")
    assert "background" not in tool.card.input_params["properties"]
    assert "description" not in tool.card.input_params["properties"]
    assert "执行 PowerShell 命令并返回输出" in tool.card.description
    assert "Start-Process" not in tool.card.description
    assert "background=true" not in tool.card.description
    assert "-Wait" not in tool.card.description
    assert "RedirectStandardOutput" not in tool.card.description
    assert "起前" not in tool.card.description
    assert "子进程" not in tool.card.description
    assert "失败换端口" not in tool.card.description
    assert "不要杀占用进程" not in tool.card.description
    original = tool.card.description
    _ensure_powershell_background_card(tool, "cn")
    assert tool.card.description == original


def test_ensure_powershell_background_card_replaces_existing_description() -> None:
    class Card:
        description = "already documents Start-Process as a URL opener"

    class Tool:
        card = Card()

    _ensure_powershell_background_card(Tool(), "cn")
    assert "Start-Process" not in Tool.card.description
    assert "执行 PowerShell 命令并返回输出" in Tool.card.description


def test_ensure_powershell_background_card_sets_compact_en_description() -> None:
    class Card:
        description = "powershell tool"

    class Tool:
        card = Card()

    _ensure_powershell_background_card(Tool(), "en")
    assert Tool.card.description.startswith("Execute a PowerShell command")
    assert "Start-Process" not in Tool.card.description
    assert "background=true" not in Tool.card.description
    assert "will not exit" not in Tool.card.description
    assert "finite long jobs" not in Tool.card.description
    assert "before start" not in Tool.card.description
    assert "child of this PID" not in Tool.card.description


def test_ensure_bash_background_card_removes_existing_property() -> None:
    existing = {
        "type": "boolean",
        "description": "upstream already exposes this",
    }

    class Card:
        input_params = {"properties": {"run_in_background": existing}}
        description = "bash tool with run_in_background already documented"

    class Tool:
        card = Card()

    _ensure_bash_background_card(Tool(), "cn")
    assert "run_in_background" not in Tool.card.input_params["properties"]
    assert "执行 Bash 命令并返回输出" in Tool.card.description


def test_patch_always_wraps_bash_init() -> None:
    class Dummy:
        def __init__(self) -> None:
            pass

        async def invoke(self, inputs):
            return None

        async def stream(self, inputs):
            if False:
                yield None

    original_init = Dummy.__init__
    _patch_tool_class(Dummy, "bash")
    assert Dummy.__init__ is not original_init
    assert getattr(Dummy.__init__, "jiuwenswarm_card_wrapped", False)
    assert getattr(Dummy.invoke, "jiuwenswarm_safety_wrapped", False)


def test_patch_always_wraps_powershell_init() -> None:
    class Dummy:
        def __init__(self) -> None:
            pass

        async def invoke(self, inputs):
            return None

        async def stream(self, inputs):
            if False:
                yield None

    _patch_tool_class(Dummy, "powershell")
    assert getattr(Dummy.__init__, "jiuwenswarm_card_wrapped", False)


def test_install_routes_generic_semicolon_command_to_bash(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: r"C:\Program Files\Git\bin\bash.exe")
    command = (
        'python --version; python -c "import reportlab; print(\'reportlab ok\')" 2>&1; '
        'python -c "import fpdf; print(\'fpdf ok\')" 2>&1'
    )

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == [r"C:\Program Files\Git\bin\bash.exe", "-lc", command]
    assert use_shell is False
    assert resolved_shell == "bash"


def test_install_routes_semicolon_command_to_powershell_without_bash(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: None)
    monkeypatch.setattr(shell_module, "_available_powershell", lambda: r"C:\Windows\powershell.exe")
    command = "python --version; python -c \"print('ok')\""

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == [r"C:\Windows\powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    assert use_shell is False
    assert resolved_shell == "powershell"


def test_install_keeps_quoted_semicolon_on_cmd(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: r"C:\Program Files\Git\bin\bash.exe")
    command = 'python -c "import reportlab; print(\'reportlab ok\')"'

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == command
    assert use_shell is True
    assert resolved_shell == "cmd"


def test_install_keeps_cmd_supported_and_separator_on_cmd(monkeypatch) -> None:
    import openjiuwen.core.sys_operation.local.shell_operation as shell_module
    from openjiuwen.core.sys_operation.shell import ShellType

    monkeypatch.setattr(shell_module, "_available_bash", lambda **_kwargs: r"C:\Program Files\Git\bin\bash.exe")
    command = "python --version && python -c \"print('ok')\""

    install_shell_tool_safety_hooks()
    plan, use_shell, resolved_shell = shell_module.ShellOperation._resolve_execution_plan(command, ShellType.AUTO)

    assert plan == command
    assert use_shell is True
    assert resolved_shell == "cmd"


def test_install_wraps_bash_tool_invoke() -> None:
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
    assert getattr(BashTool.__init__, "jiuwenswarm_card_wrapped", False)
    install_shell_tool_safety_hooks()
    assert getattr(BashTool.invoke, "jiuwenswarm_safety_wrapped", False)
    assert getattr(BashTool.__init__, "jiuwenswarm_card_wrapped", False)
