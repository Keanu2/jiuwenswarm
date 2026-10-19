# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""F1 hardening tests for ``_check_command_safety``.

Covers: line-continuation bypass fixes (bash ``\\``, powershell `` ` ``,
cmd ``^``), Rule 1 (kill family x self-identity literal), Rule 2
(name/wildcard kill of the host python interpreter with PID-precise
exemption), false-positive regressions, and the shlex-failure fallback.
"""

import sys

from jiuwenswarm.agents.harness.common.tools.command_tools import (
    _check_command_safety,
    _normalize_continuations,
    _self_identity_literals,
)


def test_normalize_continuations_bash() -> None:
    assert _normalize_continuations("pkill \\\n-f x", "bash") == "pkill -f x"
    assert _normalize_continuations("pkill \\\r\n-f x", "bash") == "pkill -f x"


def test_normalize_continuations_powershell() -> None:
    assert _normalize_continuations("pkill `\n-f x", "powershell") == "pkill -f x"


def test_normalize_continuations_cmd() -> None:
    assert _normalize_continuations("pkill ^\n-f x", "cmd") == "pkill -f x"


def test_normalize_continuations_unknown_shell_untouched() -> None:
    assert _normalize_continuations("pkill \\\n-f x", "") == "pkill \\\n-f x"


def test_normalize_continuations_shell_specific() -> None:
    # bash backtick (command substitution) must not be eaten in bash mode.
    assert _normalize_continuations("echo `pwd`", "bash") == "echo `pwd`"


def test_bash_continuation_pkill_blocked() -> None:
    assert _check_command_safety("pkill \\\n-f jiuwenswarm", "bash") is not None


def test_powershell_continuation_pkill_blocked() -> None:
    assert _check_command_safety("pkill `\n-f jiuwenswarm", "powershell") is not None


def test_cmd_continuation_pkill_blocked() -> None:
    assert _check_command_safety("pkill ^\n-f jiuwenswarm", "cmd") is not None


def test_bash_continuation_killall_blocked() -> None:
    assert _check_command_safety("killall \\\njiuwenswarm", "bash") is not None


def test_xargs_pipeline_with_continuation_blocked() -> None:
    assert _check_command_safety("pgrep -f jiuwenswarm \\\n| xargs kill", "bash") is not None


def test_stop_process_name_jiuwenswarm_blocked() -> None:
    assert _check_command_safety("Stop-Process -Name jiuwenswarm", "powershell") is not None


def test_spps_alias_jiuwenswarm_blocked() -> None:
    assert _check_command_safety("spps -Name jiuwenswarm", "powershell") is not None


def test_taskkill_jiuwenswarm_exe_blocked() -> None:
    assert _check_command_safety("taskkill /IM jiuwenswarm.exe /F", "cmd") is not None


def test_pkill_on_own_executable_path_blocked() -> None:
    assert _check_command_safety(f'pkill -f "{sys.executable}"', "bash") is not None


def test_self_identity_literals_cover_executable() -> None:
    literals = _self_identity_literals()
    assert "jiuwenswarm" in literals
    assert (sys.executable or "").lower() in literals


def test_ci_pipeline_sample_blocked() -> None:
    """Real-world sample: Get-Process python
    piped to Stop-Process -Force would kill the host runtime python."""
    command = (
        "$dir = '<team-workspace>\\backend-api'\n"
        "Get-Process python -ErrorAction SilentlyContinue |\n"
        "  Where-Object { $_.Path -like '*python*' } |\n"
        "  Stop-Process -Force -ErrorAction SilentlyContinue\n"
        "Start-Sleep -Seconds 1\n"
        'Remove-Item "$dir\\todos.db" -Force -ErrorAction SilentlyContinue'
    )
    assert _check_command_safety(command, "powershell") is not None


def test_stop_process_name_python_blocked() -> None:
    assert _check_command_safety("Stop-Process -Name python", "powershell") is not None


def test_taskkill_im_python_blocked() -> None:
    assert _check_command_safety("taskkill /IM python.exe /F", "cmd") is not None


def test_taskkill_im_python_wildcard_blocked() -> None:
    assert _check_command_safety('taskkill /IM "python*" /F', "cmd") is not None


def test_pkill_python_name_blocked() -> None:
    assert _check_command_safety("pkill python", "bash") is not None


def test_killall_python3_blocked() -> None:
    assert _check_command_safety("killall python3", "bash") is not None


def test_stop_process_by_id_allowed() -> None:
    assert _check_command_safety("Stop-Process -Id 1234", "powershell") is None


def test_stop_process_by_id_with_erroraction_allowed() -> None:
    assert (
            _check_command_safety("Stop-Process -Id 1234 -ErrorAction SilentlyContinue", "powershell")
            is None
    )


def test_bash_kill_by_pid_allowed() -> None:
    assert _check_command_safety("kill 1234", "bash") is None


def test_bash_kill_signal_pid_allowed() -> None:
    assert _check_command_safety("kill -9 1234", "bash") is None


def test_taskkill_by_pid_allowed() -> None:
    assert _check_command_safety("taskkill /PID 1234 /F", "cmd") is None


def test_enumerate_then_pid_kill_allowed_residual() -> None:
    """Documented residual: enumerate + PID kill escapes
    text matching by construction; pinned here so any future tightening is
    a conscious decision."""
    command = "Get-Process python | % { Stop-Process -Id $_.Id }"
    assert _check_command_safety(command, "powershell") is None


def test_cross_segment_mention_allowed() -> None:
    assert _check_command_safety("pkill foo; echo jiuwenswarm", "bash") is None


def test_substring_still_blocked_parity() -> None:
    # Same as the legacy regex behavior: substring match blocks.
    assert _check_command_safety("pkill -f myjiuwenswarm-test", "bash") is not None


def test_echo_kill_words_across_lines_allowed() -> None:
    assert _check_command_safety("echo pkill\necho jiuwenswarm", "bash") is None


def test_quoted_separator_not_split() -> None:
    assert _check_command_safety('echo "a;b"', "bash") is None


def test_non_interpreter_name_kill_allowed() -> None:
    assert _check_command_safety("Get-Process chrome | Stop-Process -Force", "powershell") is None


def test_stop_process_other_name_allowed() -> None:
    assert _check_command_safety("Stop-Process -Name myserver", "powershell") is None


def test_interpreter_substring_name_allowed() -> None:
    # "pythonserver" / "my-python" must not match the interpreter name rule.
    assert _check_command_safety("Stop-Process -Name pythonserver", "powershell") is None
    assert _check_command_safety("pkill -f my-python-runner", "bash") is None


def test_ordinary_commands_allowed() -> None:
    assert _check_command_safety("git status", "bash") is None
    assert _check_command_safety("npm run build", "cmd") is None


def test_unbalanced_quote_segment_falls_back_to_legacy_regex() -> None:
    assert _check_command_safety('pkill "unclosed -f jiuwenswarm', "bash") is not None


def test_single_argument_legacy_call_still_blocks() -> None:
    # Backward compatibility: shell_type omitted keeps legacy behavior.
    assert _check_command_safety("pkill -f jiuwenswarm") is not None


def test_empty_command_allowed() -> None:
    assert _check_command_safety("", "bash") is None
