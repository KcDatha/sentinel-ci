"""Codebase Analyst: maps the blast radius and seals the evidence pack.

Stage 2. Will fetch the diff between the last green commit and the
failed run's head (GitHub compare API), list the changed files, and
attach the relevant code context to the incident state so diagnosis
works from what actually changed, not from the whole repository.

Skeleton: records its place in the audit trail and passes state
through unchanged, so the graph runs end to end before Stage 2 lands.
"""

from __future__ import annotations

from ..state import SentinelCIState, audit_event


def codebase_analyst(state: SentinelCIState) -> dict:
    audit = list(state.get("audit", []))
    audit.append(
        audit_event(
            "Codebase Analyst",
            "evidence_pack_pending",
            "diff and blast radius arrive in Stage 2",
        )
    )
    return {"audit": audit}
