# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Manual smoke: register a blank teammate into the remote A2X registry.

Bypasses the broken jiuwenswarm.agents.harness.team/__init__.py
(pre-existing openjiuwen version mismatch on develop branch) by loading
a2x_registry_runtime.py directly.

Run: python test_register_teammate.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import types

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

# Stub the team package so its __init__.py (which hits the openjiuwen import
# error) does not run. Only the a2x subpackage is needed.
pkg = types.ModuleType("jiuwenswarm.agents.harness.team")
pkg.__path__ = ["jiuwenswarm/agents/harness/team"]
sys.modules["jiuwenswarm.agents.harness.team"] = pkg

spec = importlib.util.spec_from_file_location(
    "jiuwenswarm.agents.harness.team.a2x.a2x_registry_runtime",
    "jiuwenswarm/agents/harness/team/a2x/a2x_registry_runtime.py",
)
runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runtime
spec.loader.exec_module(runtime)

CONFIG_BASE = {
    "team": {"runtime": {"mode": "distributed", "role": "teammate"}},
    "react": {
        "a2x_registry": {
            "base_url": "http://121.37.53.35:8000",
            "timeout": 15.0,
            "role": "teammate",
            "dataset": "team_pool",
            "endpoint": "http://127.0.0.1:19100/a2a",
            "tunnel_device_id": "jws-teammate-test-01",
        }
    },
}


async def main() -> None:
    ok = await runtime.register_teammate_blank_agent_at_startup(
        CONFIG_BASE, source="manual-test", timeout=20.0,
    )
    print(f"\n[smoke] registration ok = {ok}")


if __name__ == "__main__":
    asyncio.run(main())
