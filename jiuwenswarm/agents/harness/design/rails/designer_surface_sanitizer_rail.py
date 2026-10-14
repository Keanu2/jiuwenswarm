# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Design leader surface sanitizer (team.design only, single-repo policy).

The design leader is assembled through the standard agent-core configurator,
which unconditionally mounts a few capability surfaces the design canvas has
no use for. Rather than patching agent-core, this rail **subtracts** them
after the fact — all removal happens in jiuwenswarm, so tuning the design
leader's surface is a single-repo change.

What it strips and why (verified against the real leader surface probe,
2026-10-10):

* ``workspace_meta`` — appended by ``TeamToolRail.init`` *after*
  ``create_team_tools`` already applied ``exclude_tools``, so the team-level
  exclude list (``TeamAgentSpec.team_tool_exclude``) cannot reach it. The
  design leader never manages the shared deliverables workspace.
* ``org_*`` control tools — mounted by
  ``TeamRuntimeManager.activate → OrganizationRuntimeManager.ensure_control_tools``
  onto every running leader (harness.add_tool), bypassing the tool factory
  entirely. The design leader has no organization surface.
* ``group_send_message`` — the group-chat tool whose card name differs from
  the plain ``send_message`` entry already in the exclude list. Covered here
  too (belt and braces) so the design exclude list stays a plain name set.

Timing (why init is enough):
  rails initialize lazily in priority-descending order on the first stream
  use — *after* activate() already mounted the org tools. This rail uses a
  low priority (10) so it initializes last and sees every polluted surface.
  Native rebuilds re-run init, and the removals are idempotent.

It also defensively drops the ``team_bootstrap`` prompt section on every
``before_model_call``: agent-core's ``leader_bootstrap=False`` spec flag
already keeps it off, but TeamPolicyRail re-adds its sections each model
call, and a future agent-core change re-introducing the section would
instruct the design leader to call ``build_team`` — a tool it does not have.
"""

from __future__ import annotations

import logging
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

# Tools removed from the design leader's surface. Every name is a tool *card
# name* as registered by ability_manager.add_ability.
_REMOVED_TOOL_NAMES: tuple[str, ...] = (
    # Shared-workspace management (appended post-factory in TeamToolRail).
    "workspace_meta",
    # Group chat (companion of the already-excluded send_message).
    "group_send_message",
    # Organization bootstrap control (mounted by ensure_control_tools).
    "org_create_organization",
    "org_invite_team",
    "org_dissolve_organization",
    "org_list_available_teams",
    "org_list_configured_teams",
    "org_activate_and_invite_team",
    "org_list_expert_groups",
    "org_create_and_invite_expert_team",
    "org_view_organization",
)

# Organization collaboration tools (leader toolset from create_org_leader_tools
# when an org_task_manager is bound; same bypass concern as above).
_ORG_LEADER_TOOL_NAMES: tuple[str, ...] = (
    "org_view_tasks",
    "org_create_task",
    "org_claim_task",
    "org_delegate_task",
    "org_update_task",
    "org_send_leader_message",
    "org_get_leader_message",
    "org_list_leader_messages",
    "org_ack_leader_message",
    "org_view_child_tasks",
    "org_view_pending_reviews",
    "org_review_task",
    "org_create_summary_execution",
    "org_summary_get_inputs",
    "org_summary_complete",
)

# Prompt sections defensively removed on every model call. ``team_bootstrap``
# is the build_team instruction — the design leader has no build_team tool.
_REMOVED_SECTIONS: tuple[str, ...] = ("team_bootstrap",)


class DesignerSurfaceSanitizerRail(DeepAgentRail):
    """Subtract non-design capabilities from the design leader surface."""

    # Low priority: initialize (and run before_model_call) *after* the core
    # team rails (TeamToolRail=90, TeamPolicyRail=12) so every removal sees
    # the fully assembled surface.
    priority = 10

    def __init__(self) -> None:
        super().__init__()
        self._removed_tools: list[str] = []
        self._agent: Any = None
        self.system_prompt_builder = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _remove_tools(self, agent: Any) -> None:
        """Drop every listed tool card from the ability manager (idempotent)."""
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is None:
            return
        for name in (*_REMOVED_TOOL_NAMES, *_ORG_LEADER_TOOL_NAMES):
            try:
                if getattr(ability_manager, "remove_ability", None) is not None:
                    ability_manager.remove_ability(name)
                else:  # older cores without remove_ability
                    ability_manager.remove(name)
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[DesignerSurfaceSanitizer] tool %s already absent", name
                )
                continue
            self._removed_tools.append(name)

    # ------------------------------------------------------------------
    # rail lifecycle
    # ------------------------------------------------------------------

    def init(self, agent: Any) -> None:
        super().init(agent)
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._agent = agent
        self._remove_tools(agent)
        if self._removed_tools:
            logger.info(
                "[DesignerSurfaceSanitizer] removed %d tools from the design "
                "leader surface: %s",
                len(self._removed_tools),
                sorted(set(self._removed_tools)),
            )
            self._removed_tools = []

    def uninit(self, agent: Any) -> None:
        # Nothing to restore: the removed tools belong to rails that manage
        # their own teardown, and the harness is being torn down anyway.
        self.system_prompt_builder = None
        self._agent = None
        self._removed_tools = []

    async def before_model_call(self, ctx: Any) -> None:
        _ = ctx
        # Defensive re-removal: a native rebuild re-mounts the dynamic tool
        # registry (TeamHarness._remount_dynamic_tools restores org_* tools)
        # before this rail re-initializes; if any mounting path slipped a
        # polluted tool back in between init and this model call, drop it
        # again. Idempotent and cheap (dict lookups).
        if self._agent is not None:
            self._remove_tools(self._agent)
        builder = self.system_prompt_builder
        if builder is None:
            return
        for section in _REMOVED_SECTIONS:
            try:
                builder.remove_section(section)
            except Exception:  # noqa: BLE001
                continue


__all__ = ["DesignerSurfaceSanitizerRail"]
