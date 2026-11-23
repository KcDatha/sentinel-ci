"""Repair: draft the smallest safe fix.

Stage 2 drafts the patch; Stage 3 is what makes it trustworthy. Per
the SENTINEL pattern a repair proposal carries the change itself, its
blast radius, a rollback path, and regression coverage. The Repair
agent never opens a pull request: only a fix that survives Adversarial
Verification reaches the controller's pull request step (Stage 4).

Skeleton: drafts nothing and counts the attempt, so the routing in
the graph (retry on failed verification, capped, then escalate) is
exercisable before real patches exist.
"""

from __future__ import annotations

from ..state import SentinelCIState, audit_event


def repair(state: SentinelCIState) -> dict:
    audit = list(state.get("audit", []))
    audit.append(
        audit_event(
            "Repair",
            "draft_pending",
            "patch drafting arrives in Stage 2",
        )
    )
    return {
        "audit": audit,
        "repair_attempts": state.get("repair_attempts", 0) + 1,
        "status": "verifying",
    }
