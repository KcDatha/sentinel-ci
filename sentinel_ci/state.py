"""Typed state shared by every SENTINEL CI agent.

Following the SENTINEL architecture, all agents read and write one
typed state object, so every conclusion traces back to its evidence.
The schema is a plain ``TypedDict`` so it drops directly into a
LangGraph ``StateGraph`` and stays usable without LangGraph installed
(the fallback runner in :mod:`sentinel_ci.graph` consumes the same
shape).
"""

from __future__ import annotations

from typing import TypedDict


class Hypothesis(TypedDict, total=False):
    """One candidate cause, with the observation that would kill it."""

    cause: str
    supporting_evidence: list[str]
    falsifier: str
    confidence: float


class AuditEvent(TypedDict, total=False):
    """One entry in the incident audit trail."""

    agent: str
    action: str
    detail: str


class SentinelCIState(TypedDict, total=False):
    # --- Incident identity (set by the watcher / Mission Controller) ---
    repo: str
    run_id: int
    workflow_name: str
    head_branch: str
    head_sha: str
    base_sha: str  # last green commit on the same branch, when known

    # --- Evidence (Stage 1 parser, Stage 2 Codebase Analyst) ---
    log_text: str
    failing_tests: list[str]
    error_excerpts: list[str]
    failure_summary: str
    exit_code: int | None
    changed_files: list[str]
    diff: str

    # --- Diagnosis (Stage 2) ---
    hypotheses: list[Hypothesis]
    root_cause: str
    root_cause_confidence: float

    # --- Repair (Stage 2) ---
    repair_patch: str
    repair_attempts: int

    # --- Verification gate (Stage 3) ---
    verification_verdict: str  # "passed" | "failed" | "inconclusive"
    verification_notes: list[str]

    # --- Outcome ---
    # queued -> diagnosing -> repairing -> verifying -> ready_for_pr
    # any stage can divert to "escalated" (human in the loop).
    status: str
    escalation_reason: str
    pr_url: str

    # --- Auditability by construction ---
    audit: list[AuditEvent]


def new_state(**overrides) -> SentinelCIState:
    """A state with safe defaults; keyword arguments override fields."""
    state: SentinelCIState = {
        "failing_tests": [],
        "error_excerpts": [],
        "changed_files": [],
        "hypotheses": [],
        "root_cause_confidence": 0.0,
        "repair_attempts": 0,
        "verification_notes": [],
        "status": "queued",
        "audit": [],
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def audit_event(agent: str, action: str, detail: str = "") -> AuditEvent:
    return {"agent": agent, "action": action, "detail": detail}
