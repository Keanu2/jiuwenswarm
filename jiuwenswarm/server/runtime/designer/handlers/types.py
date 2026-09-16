# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from jiuwenswarm.common.schema.designer_graph import (
    AssetRef,
    DesignerExecutionGraph,
    DesignerExecutionRun,
)

ActivityEmitter = Callable[..., None]


@dataclass(frozen=True)
class NodeExecutionContext:
    graph: DesignerExecutionGraph
    run_id: str
    node_id: str
    run: DesignerExecutionRun | None = None
    emit_activity: ActivityEmitter | None = None


@dataclass(frozen=True)
class NodeResult:
    output_ref: AssetRef | None = None
    output_refs: list[AssetRef] | None = None
    message: str = ""
