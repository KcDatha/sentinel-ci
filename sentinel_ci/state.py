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
    confidence_note: str


class CodeRegion(TypedDict, total=False):
    """One bounded slice of source code collected as evidence."""

    path: str
    content: str
    note: str  # how the slice was bounded, e.g. "lines 12-72 of 210"


class AuditEvent(TypedDict, total=False):
    """One entry in the incident audit trail."""

    agent: str
    action: str
    detail: str


class AdversaryReport(TypedDict, total=False):
    """The Adversarial Verification agent's structured attack report."""

    verdict: str  # "survives" | "broken"
    attack: str  # the strongest case it could make against the fix
    reasoning: str


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
    code_context: list[CodeRegion]
    diff_truncated: bool
    context_truncated: bool

    # --- Diagnosis (Stage 2) ---
    hypotheses: list[Hypothesis]
    hypothesis_attempts: int
    rejected_causes: list[str]  # causes Root Cause killed; never re-proposed
    root_cause_verdict: str  # "confirmed" | "rejected" | ""
    root_cause: str
    root_cause_confidence: float

    # --- Repair (Stage 2) ---
    repair_patch: str
    repair_files: list[str]  # files the accepted patch touches
    repair_attempts: int

    # --- Verification gate (Stage 3) ---
    # repo_path is the local checkout of the incident repository; the
    # gate checks head_sha out into an isolated sandbox from it and
    # never mutates it. Without a checkout the gate cannot run and
    # the incident stays held at awaiting_verification.
    repo_path: str
    sandbox_mode: str  # "docker" | "worktree" | "" (gate has not run)
    targeted_failures: list[str]  # originally failing tests still red
    new_failures: list[str]  # suite failures the failing run did not have
    verification_output: str  # bounded tail of the sandbox test output
    adversary: AdversaryReport
    verification_verdict: str  # "passed" | "failed" | "inconclusive"
    verification_notes: list[str]

    # --- Outcome ---
    # queued -> diagnosing -> awaiting_verification -> verifying ->
    # verified; any stage can divert to "escalated" (human in the
    # loop). "verified" means all three Stage 3 gate conditions
    # passed and the fix is ready for Stage 4 to open the pull
    # request. "awaiting_verification" is where an incident with a
    # proposed patch but no runnable gate (no local checkout) stays
    # held: not trusted, not shipped.
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
        "code_context": [],
        "diff_truncated": False,
        "context_truncated": False,
        "hypotheses": [],
        "hypothesis_attempts": 0,
        "rejected_causes": [],
        "root_cause_verdict": "",
        "root_cause_confidence": 0.0,
        "repair_files": [],
        "repair_attempts": 0,
        "sandbox_mode": "",
        "targeted_failures": [],
        "new_failures": [],
        "adversary": {},
        "verification_notes": [],
        "status": "queued",
        "audit": [],
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


def audit_event(agent: str, action: str, detail: str = "") -> AuditEvent:
    return {"agent": agent, "action": action, "detail": detail}
