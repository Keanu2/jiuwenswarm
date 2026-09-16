# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Offline A/B experiment modules (not wired into browser bootstrap).

Plan A — director beat/blocking contract (domain-agnostic) + v2 locks.
Plan B — master-still keyframe policy (solos as identity refs only).
"""

from jiuwenswarm.server.runtime.designer.experiments.director_contract import (
    apply_director_contract,
)
from jiuwenswarm.server.runtime.designer.experiments.keyframe_policy import (
    apply_compose_solos_setting_policy,
    apply_ensemble_master_policy,
    apply_master_still_policy,
)
from jiuwenswarm.server.runtime.designer.experiments.plan_a_v2 import apply_plan_a_v2

__all__ = [
    "apply_director_contract",
    "apply_compose_solos_setting_policy",
    "apply_ensemble_master_policy",
    "apply_master_still_policy",
    "apply_plan_a_v2",
]
