# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""team.design worker backend wiring — the TeamWorkerBackend integration.

User ruling (2026-10-12, terminal state): ALL worker logic lives in the
designer AgentGroup package (persona / tools / per-role tool surfaces); the
claw repo only compiles graph scripts (node → ``agent(options={
'agent_template': role})``) and reads events. This module is that thin
mapping on the execution side:

* :func:`build_worker_backend` — construct the agent-core
  ``TeamWorkerBackend`` with the design team's teammate base spec and the
  role-template ``spec_resolver`` below;
* :func:`resolve_role_spec` — map an ``agent_template`` reference
  (``"designer/storyboard"``) + the base teammate spec to the worker
  ``DeepAgentSpec`` that call should derive from: the role template's
  persona (flattened prompt sections) becomes the system prompt and its
  manifest tools (``harness.tool.file`` specs into the package's shared
  designer worker tools module) ride the spec so ``TeamHarness.build``
  instantiates them per worker.

Media/config hard-coding lives inside the package tool module
(``tools/designer_worker_tools.py`` calls the claw handler functions
directly); the follow-up crew will strip those out of the claw repo.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DESIGNER_GROUP = "designer"


def resolve_role_spec(template_ref: str, base_spec: Any) -> Any:
    """``agent_template`` reference → worker ``DeepAgentSpec``.

    The reference is ``"<group>/<member>"`` (e.g. ``"designer/clip"``).
    Unknown references return the base spec unchanged (TeamWorkerBackend
    contract: the embedder decides whether that is an error).
    """
    from jiuwenswarm.agents.swarm.agent_group import load_agent_group_package
    from jiuwenswarm.server.runtime.designer.node_agent import (
        flatten_template_prompt,
    )

    group_name, _, member_id = str(template_ref or "").partition("/")
    group_name = group_name.strip() or _DESIGNER_GROUP
    member_id = member_id.strip()
    if not member_id:
        return base_spec
    try:
        from jiuwenswarm.server.runtime.extension_package_manager import (
            install_agent_group,
            resolve_agent_group_dir,
        )

        try:
            package_dir = resolve_agent_group_dir(group_name)
        except Exception:  # noqa: BLE001 — not installed yet (fresh env)
            if group_name != _DESIGNER_GROUP:
                raise
            install_agent_group({"id": _DESIGNER_GROUP})
            package_dir = resolve_agent_group_dir(group_name)
        template = load_agent_group_package(Path(package_dir)).get(member_id)
    except (OSError, ValueError):
        logger.exception("role template resolution failed: %s", template_ref)
        return base_spec
    if template is None:
        logger.warning("role template not found: %s (using base spec)", template_ref)
        return base_spec
    persona = flatten_template_prompt(template)
    update: dict[str, Any] = {"tools": list(template.tools) + list(base_spec.tools or [])}
    # Skill visibility seed: template skill names ride the spec (merged with
    # the base spec's, same rule as the assembly path). The rail only reads
    # names from the global skill library — the SKILL.md files themselves are
    # not read out of the package by this path.
    skill_names: list[str] = []
    for name in [
        *(base_spec.skills or []),
        *(Path(skill.dir).name for skill in template.skills),
    ]:
        if name and name not in skill_names:
            skill_names.append(name)
    if skill_names:
        update["skills"] = skill_names
    if persona:
        update["system_prompt"] = persona
    card = getattr(template, "agent_card", None)
    if card is not None:
        update["description"] = str(getattr(card, "description", "") or member_id)
    # Ticket-binding rail (colleague sample §3.6 semantics): parses the node
    # ticket off every model call's input and binds it onto the shared tool
    # context, so the package tools never depend on the model relaying a
    # ticket argument. Attached programmatically here (one source of truth
    # in the group package's rails/ dir; role manifests stay tool-only).
    try:
        update["rails"] = [
            *(base_spec.rails or []),
            _ticket_rail_spec(Path(package_dir)),
        ]
    except Exception:  # noqa: BLE001 — rail attach is best-effort
        logger.debug("ticket rail attach failed", exc_info=True)
    return base_spec.model_copy(update=update)


def _ticket_rail_spec(package_dir: Path) -> Any:
    """Build the harness.rail.file spec for the ticket-binding rail."""
    from openjiuwen.harness.schema.deep_agent_spec import RailSpec

    rail_path = package_dir / "rails" / "node_ticket_binding_rail.py"
    if not rail_path.is_file():
        raise FileNotFoundError(rail_path)
    return RailSpec(
        type="harness.rail.file",
        params={"file_path": str(rail_path), "class_name": "NodeTicketBindingRail"},
    )


def build_worker_backend(
    *,
    team_name: str,
    worker_base_spec: Any,
    build_context: Any,
    run_id: str,
) -> Any:
    """Construct the agent-core TeamWorkerBackend for one design trigger."""
    from openjiuwen.agent_teams.workflow.backends.team_worker_backend import (
        TeamWorkerBackend,
    )

    return TeamWorkerBackend(
        model=None,
        team_name=team_name,
        worker_base_spec=worker_base_spec,
        build_context=build_context,
        run_id=run_id,
        spec_resolver=resolve_role_spec,
    )


__all__ = ["build_worker_backend", "resolve_role_spec"]
