# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Opt-in: real sandbox account and host-declared tools; no cloud/downloads.

Provide JIUWENBOX_TEST_SHARED_TOOLS=1, JIUWENBOX_TEST_PYTHON/NODE/BASH,
JIUWENBOX_TOOL_PATHS and JIUWENBOX_PATH_ALLOWLIST. No product path discovery.
"""

import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from ctypes import wintypes
from pathlib import Path

import pytest

from jiuwenbox.server import shared_paths
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.supervisor import win_acl, win_setup


def _run_as_sandbox(command, env, cwd):
    from jiuwenbox.supervisor.win_exec import STARTUPINFOW, ProcessInfo, _get_advapi32

    password = win_setup.get_sandbox_user_password()
    assert password, "installed sandbox credential unavailable"
    startup = STARTUPINFOW()
    startup.cb = ctypes.sizeof(startup)
    info = ProcessInfo()
    block = ctypes.create_unicode_buffer("\0".join(f"{k}={v}" for k, v in sorted(env.items())) + "\0\0")
    ok = _get_advapi32().CreateProcessWithLogonW(
        "jbx-sandbox", None, password, 1, command[0],
        ctypes.create_unicode_buffer(subprocess.list2cmdline(command)),
        0x08000000 | 0x400, ctypes.cast(block, ctypes.c_void_p), str(cwd),
        ctypes.byref(startup), ctypes.byref(info),
    )
    password = None
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    try:
        if kernel.WaitForSingleObject(info.hProcess, 90000) != 0:
            kernel.TerminateProcess(info.hProcess, 24)
            raise RuntimeError("sandbox tool probe timed out")
        code = wintypes.DWORD()
        assert kernel.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        kernel.CloseHandle(info.hThread)
        kernel.CloseHandle(info.hProcess)


@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("JIUWENBOX_TEST_SHARED_TOOLS") != "1",
                    reason="explicitly opt in to host-declared tool ACL updates")
def test_actual_sandbox_can_install_and_import_in_writable_venv(tmp_path, monkeypatch, request):
    from jiuwenbox.server.sandbox_manager import _build_windows_exec_env
    monkeypatch.setenv("JIUWENBOX_ENV_ALLOWLIST", json.dumps(["JIUWENBOX_TEST_PYTHON", "JIUWENBOX_TEST_NODE"]))
    sid = win_setup.get_sandbox_user_sid()
    assert sid, "jbx-sandbox must already be installed"
    roots, writable = shared_paths.shared_tool_paths()
    _, directories = shared_paths.configured_tool_paths()
    assert writable
    assert len(roots) >= 3, "declare writable venv, base Python and Node"
    bash = os.environ.get("JIUWENBOX_TEST_BASH") or shutil.which("bash")
    assert bash, "provide a Bash executable"
    policy = {"allow_read": [str(tmp_path), *roots], "allow_write": [str(tmp_path), *writable],
              "deny_read": [], "deny_write": []}
    win_acl.apply_sandbox_acl(str(tmp_path), [str(tmp_path)], [], allow_read=[str(tmp_path)],
                              sandbox_user_sid=sid, bounded_roots=[str(tmp_path)])
    win_acl.grant_parent_traverse(str(tmp_path), sid, filesystem_policy=policy)
    monkeypatch.setattr(shared_paths, "_shared_acl_cache_path", lambda: tmp_path / "shared-tools-acl.json")
    began = time.monotonic()
    shared_paths.prepare_shared_tool_acl(SecurityPolicy(version=1))
    cold_seconds = time.monotonic() - began
    with monkeypatch.context() as warm:
        def unexpected_walk(*args, **kwargs):
            raise AssertionError("unchanged startup walked the tool tree")
        warm.setattr(win_acl, "_walk_acl_objects", unexpected_walk)
        began = time.monotonic()
        shared_paths.prepare_shared_tool_acl(SecurityPolicy(version=1))
        warm_seconds = time.monotonic() - began
    name = "jbx_acl_probe_" + uuid.uuid4().hex
    wheel = tmp_path / f"{name}-0.0.1-py3-none-any.whl"
    dist = f"{name}-0.0.1.dist-info"
    files = {
        f"{name}/__init__.py": "VALUE = 'pip-installed'\n",
        f"{dist}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: 0.0.1\n",
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files[f"{dist}/RECORD"] = "".join(f"{file},,\n" for file in [*files, f"{dist}/RECORD"])
    with zipfile.ZipFile(wheel, "w") as archive:
        for file, content in files.items():
            archive.writestr(file, content)
    result_file = tmp_path / "result.json"
    # Keep this output host-owned: new sandbox-owned files need not grant the
    # host account read access on every test machine's temporary-directory ACL.
    result_file.touch()
    script = tmp_path / "probe.py"
    script.write_text(f'''
import importlib, importlib.util, json, os, pathlib, subprocess, sys, traceback
out = pathlib.Path({str(result_file)!r})
try:
    assert os.getlogin().lower() == 'jbx-sandbox', os.getlogin()
    import pptx
    import pip
    for directory in {roots!r}:
        assert list(pathlib.Path(directory).iterdir()), directory
    assert (pathlib.Path(sys.executable).parent.parent / 'pyvenv.cfg').read_text()
    node = subprocess.check_output([{os.environ['JIUWENBOX_TEST_NODE']!r}, '--version'], text=True).strip()
    # Validate the same bare-name resolution a skill command uses.
    bare = subprocess.check_output([{bash!r}, '-lc', 'python -c "import sys; print(sys.executable)"'], text=True).strip().splitlines()[-1]
    assert os.path.normcase(bare) == os.path.normcase(sys.executable), (bare, sys.executable)
    bare_pip = subprocess.check_output([{bash!r}, '-lc', 'pip --version'], text=True)
    assert str(pathlib.Path(sys.prefix) / 'Lib' / 'site-packages').lower() in bare_pip.lower(), bare_pip
    subprocess.run([sys.executable, '-m', 'pip', '--version'], check=True)
    assert importlib.util.find_spec({name!r}) is None
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps',
                    '--disable-pip-version-check', {str(wheel)!r}], check=True)
    installed = importlib.import_module({name!r})
    assert installed.VALUE == 'pip-installed'
    assert pathlib.Path(installed.__file__).is_relative_to(sys.prefix)
    # A fresh skill process imports both packages without any PYTHONPATH edits.
    assert 'PYTHONPATH' not in os.environ
    subprocess.run([os.environ['JIUWENBOX_TEST_PYTHON'], '-c',
                    'import {name}, pptx; assert {name}.VALUE == "pip-installed"'], check=True)
    # Future dependency directories inherit the common root's RWX permission.
    created = pathlib.Path({writable[0]!r}) / '{name}_future_deps'
    try:
        (created / 'nested').mkdir(parents=True)
        future_file = created / 'nested' / 'probe.txt'
        future_file.write_text('inherited')
        subprocess.run([sys.executable, '-c',
                        'from pathlib import Path; p = Path(' + repr(str(future_file)) + '); '
                        'assert p.read_text() == "inherited"; p.write_text("updated")'], check=True)
    finally:
        import shutil
        shutil.rmtree(created)
    for readonly in {[root for root in roots if not any(Path(root).is_relative_to(w) for w in writable)]!r}:
        try:
            with (pathlib.Path(readonly) / '{name}.denied').open('w'):
                pass
        except PermissionError:
            pass
        else:
            raise AssertionError('base Python/Node must remain read-only')
    out.write_text(json.dumps(dict(user=os.getlogin(), python=sys.executable,
                   pptx=pptx.__version__, node=node, directories_readable=True,
                   pip_install=True, direct_import=True, fresh_process_import=True, future_dependency_dir=True,
                   installed_file=installed.__file__, preset_file=pptx.__file__)))
except BaseException:
    out.write_text(traceback.format_exc())
    raise
''', encoding="utf-8")
    env = _build_windows_exec_env({
        "SystemRoot": os.environ["SystemRoot"], "TEMP": str(tmp_path), "TMP": str(tmp_path),
        "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
    })
    assert env["PATH"].split(os.pathsep)[:len(directories)] == directories
    def cleanup():
        # Remove only this UUID-named probe package, preserving real skill deps.
        code = _run_as_sandbox([
            os.environ["JIUWENBOX_TEST_PYTHON"], "-c",
            f"import subprocess, sys; sys.exit(subprocess.call([sys.executable, '-m', 'pip', "
            f"'uninstall', '--yes', '--disable-pip-version-check', {name!r}]))",
        ], env, tmp_path)
        assert code == 0, "sandbox probe package cleanup failed"
    request.addfinalizer(cleanup)
    code = _run_as_sandbox([env["JIUWENBOX_TEST_PYTHON"], str(script)], env, tmp_path)
    output = result_file.read_text() if result_file.exists() else "probe produced no result"
    assert code == 0, f"exit={code}; {output}"
    result = json.loads(output)
    assert result["user"].lower() == "jbx-sandbox"
    assert result["pip_install"] and result["direct_import"] and result["fresh_process_import"]
    session = tmp_path / "session-workspace"
    session.mkdir()
    win_acl.apply_sandbox_acl(str(session), [str(session)], [], sandbox_user_sid=sid,
                              bounded_roots=[str(session)], preserve_roots=roots)
    # Even legacy cleanup lists containing shared roots cannot revoke them.
    win_acl.revoke_sandbox_acl([str(session), *roots], sandbox_user_sid=sid, preserve_roots=roots)
    code = _run_as_sandbox([
        env["JIUWENBOX_TEST_PYTHON"], "-c",
        f"import {name}, pptx; assert {name}.VALUE == 'pip-installed'",
    ], env, tmp_path)
    assert code == 0, "shared imports failed after session stop"
    result.update(cold_acl_seconds=round(cold_seconds, 3), warm_acl_seconds=round(warm_seconds, 3),
                  warm_tree_walks=0, import_after_session_stop=True)
    print(json.dumps(result, ensure_ascii=False))
