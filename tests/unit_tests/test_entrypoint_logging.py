# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Verify entrypoint log routing without starting services or touching user data."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("entrypoint", ["server/app_agentserver.py", "gateway/app_gateway.py"])
@pytest.mark.parametrize("layout", ["standalone", "desktop", "desktop_outer_date"])
@pytest.mark.parametrize("explicit_config", [False, True])
def test_agent_core_warning_and_error_files(tmp_path, entrypoint, layout, explicit_config):
    repo = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    for key in (
        "JIUWENSWARM_LOG_DIR", "JIUWENSWARM_CORE_LOG_DIR",
        "JIUWENSWARM_LOG_DATE_ROOT", "LOG_LEVEL",
    ):
        env.pop(key, None)
    env["JIUWENSWARM_DATA_DIR"] = str(tmp_path)
    if layout != "standalone":
        logs_dir = tmp_path / "swarm"
        if layout == "desktop_outer_date":
            env["JIUWENSWARM_LOG_DATE_ROOT"] = str(tmp_path / "logs")
            logs_dir = tmp_path / "logs" / "jiuwenswarm" / "test-user"
        env["JIUWENSWARM_LOG_DIR"] = str(logs_dir)
        env["JIUWENSWARM_CORE_LOG_DIR"] = str(logs_dir / "core")

    # Run the actual logging branch in a child: importing the whole entrypoint
    # also initializes workspaces, installs skills, and imports service runtimes.
    script = textwrap.dedent(r"""
        import ast
        import asyncio
        import logging
        import os
        from pathlib import Path
        import sys
        from typing import Any
        from types import SimpleNamespace

        repo, root = map(Path, sys.argv[1:3])
        sys.path.insert(0, str(repo))
        config = root / 'config' / 'logging.yaml'
        config.parent.mkdir()
        explicit = sys.argv[4] == 'True'
        if explicit:
            config.write_text(
                'logging:\n  level: ERROR\n  output: [file]\n  log_path: '
                + (root / 'custom-core').as_posix() + '\n', encoding='utf-8')

        from jiuwenswarm.common import utils
        from openjiuwen.core.common.logging import LogManager

        utils.configure_agent_core_log_dir()
        LogManager.get_logger('interface')  # Registered before the fallback.
        path = repo / 'jiuwenswarm' / sys.argv[3]
        tree = ast.parse(path.read_text(encoding='utf-8'))
        branch = next(node for node in tree.body if isinstance(node, ast.If)
                      and ast.unparse(node.test) == '_logging_yaml.exists()')
        exec(compile(ast.Module(body=[branch], type_ignores=[]), str(path), 'exec'),
             {'_logging_yaml': config, 'logging': logging, 'LogManager': LogManager})
        utils.configure_agent_core_log_dir()

        names = ('common', 'interface', 'prompt_builder', 'performance')
        for name in names:
            logger = LogManager.get_logger(name)
            logger.warning('regression-warning-' + name)
            logger.error('regression-error-' + name)
        if not explicit and sys.argv[3].startswith('server/'):
            LogManager.get_logger('common').info('[PermissionEngine] regression-permission')

        # Exercise the actual command.mcp skip branch, not just a synthetic
        # common-logger warning. No MCP transport or service is started.
        path = repo / 'jiuwenswarm/server/agent_ws_server.py'
        tree = ast.parse(path.read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == 'AgentWebSocketServer')
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                      and n.name == '_fetch_mcp_tools_from_config')
        from openjiuwen.core.common.logging import logger as core_logger
        ns = {'Any': Any, 'asyncio': asyncio, 'core_logger': core_logger}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), ns)
        fetch = ns['_fetch_mcp_tools_from_config'].__func__
        asyncio.run(fetch({'name': 'regression', 'transport': 'stdio'}))

        # Run the handler's uncaught-exception branch as well as skip warnings.
        from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
        handler = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                       and n.name == '_handle_command_mcp')
        def fail_servers():
            raise RuntimeError('regression-command-mcp-failure')
        async def send_payload(ws, wire):
            ws.append(wire)
        ns.update(AgentRequest=AgentRequest, AgentResponse=AgentResponse,
                  get_mcp_servers=fail_servers, send_wire_payload=send_payload,
                  encode_agent_response_for_wire=lambda response, response_id: response)
        exec(compile(ast.Module(body=[handler], type_ignores=[]), str(path), 'exec'), ns)
        async def run_handler():
            responses = []
            request = SimpleNamespace(params={'action': 'list'}, request_id='logging-failure', channel_id='web')
            await ns['_handle_command_mcp'](object(), responses, request, asyncio.Lock())
            assert not responses[0].ok and responses[0].payload['code'] == 'MCP_INTERNAL'
        asyncio.run(run_handler())
        logging.shutdown()

        files = list(root.rglob('*.log'))
        core_dir = ('core' if os.environ.get('JIUWENSWARM_CORE_LOG_DIR')
                    else ('custom-core' if explicit else 'logs'))
        core_files = [p for p in files if core_dir in p.relative_to(root).parts]
        content = '\n'.join(p.read_text(encoding='utf-8') for p in core_files)
        for name in names:
            assert ('regression-warning-' + name in content) == (not explicit), name
            assert 'regression-error-' + name in content, name
        command_warning = '[command.mcp] _fetch skipped: no command for stdio'
        assert (command_warning in content) == (not explicit)
        assert '[command.mcp] failed: regression-command-mcp-failure' in content
        if not explicit:
            assert any(command_warning in p.read_text(encoding='utf-8')
                       for p in core_files if p.name == 'jiuwen.log')
        if not explicit and sys.argv[3].startswith('server/'):
            assert any('regression-permission' in p.read_text(encoding='utf-8')
                       for p in files if p.name == 'permissions.log')
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo), str(tmp_path), entrypoint, str(explicit_config)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
