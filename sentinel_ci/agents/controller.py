"""Mission Controller: owns the incident and the routing.

In the SENTINEL pattern the controller classifies the failure,
assembles incident state, and routes forward on confidence and
backward on doubt. In SENTINEL CI the controller is the hub of the
graph: it opens the incident (parsing raw logs with the Stage 1
parser when needed) and it closes it, either by marking a verified
fix ready for a pull request (Stage 4 opens it) or by escalating to a
human with the evidence trail attached when the verification gate
cannot pass within the attempt budget.
"""

from __future__ import annotations

from ..log_parser import parse_log_text
from ..state import SentinelCIState, audit_event

DEFAULT_MAX_REPAIR_ATTEMPTS = 2


def controller(
    state: SentinelCIState, max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS
) -> dict:
    updates: dict = {}
    audit = list(state.get("audit", []))

    # Incident assembly: if raw logs arrived without a parsed summary
    # (for example a state built by hand), parse them now so every
    # downstream agent works from the same evidence.
    if state.get("log_text") and not state.get("failure_summary"):
        summary = parse_log_text(state["log_text"])
        updates.update(
            failing_tests=summary.failing_tests,
            error_excerpts=summary.error_excerpts,
            failure_summary=summary.summary_line,
            exit_code=summary.exit_code,
        )
        audit.append(audit_event("Mission Controller", "log_parsed", summary.summary_line))

    verdict = state.get("verification_verdict")
    if verdict == "passed" and state.get("status") != "ready_for_pr":
        updates["status"] = "ready_for_pr"
        audit.append(
            audit_event(
                "Mission Controller",
                "gate_passed",
                "verification survived; fix is ready for a pull request",
            )
        )
    elif verdict in {"failed", "inconclusive"} and (
        state.get("repair_attempts", 0) >= max_repair_attempts
    ):
        updates["status"] = "escalated"
        updates["escalation_reason"] = (
            f"Verification gate did not pass after "
            f"{state.get('repair_attempts', 0)} repair attempt(s); "
            "a human gets the full evidence trail."
        )
        audit.append(
            audit_event("Mission Controller", "escalated", updates["escalation_reason"])
        )

    updates["audit"] = audit
    return updates
