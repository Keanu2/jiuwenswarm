# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Role-local tool wrappers for the designer AgentGroup.

The agent-template loader restricts manifest ``tools[].file`` references to
the agent directory (package-root escape rule), while the real tool module
lives at the package level (``<group>/tools/designer_worker_tools.py``) so
every role shares ONE implementation. This wrapper loads that module by
relative path at import time and re-exports the tool classes under the
names the manifest declares.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SOURCE = (
    Path(__file__).resolve().parent.parent.parent
    / "tools"
    / "designer_worker_tools.py"
)

_MODULE_NAME = "designer_worker_tools"

if _MODULE_NAME not in sys.modules:
    _spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SOURCE)
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_MODULE_NAME] = _module
    _spec.loader.exec_module(_module)
else:
    _module = sys.modules[_MODULE_NAME]

CallModelTool = _module.CallModelTool
GenerateImageTool = _module.GenerateImageTool
GenerateVideoTool = _module.GenerateVideoTool
ReadNodeContextTool = _module.ReadNodeContextTool
SubmitNodeOutputTool = _module.SubmitNodeOutputTool

__all__ = [
    "CallModelTool",
    "GenerateImageTool",
    "GenerateVideoTool",
    "ReadNodeContextTool",
    "SubmitNodeOutputTool",
]
