"""Root Cause: test hypotheses until one cause explains every symptom.

Stage 2. Will score each hypothesis against the evidence (failing
tests, excerpts, diff) and keep only a cause that explains all of it,
with a calibrated confidence. The graph routes on that confidence:
below the configured threshold the incident escalates instead of
guessing forward.

Skeleton: with no hypotheses to test, confidence stays at zero and the
incident escalates, by design.
"""

from __future__ import annotations

from ..state import SentinelCIState, audit_event


def root_cause(state: SentinelCIState) -> dict:
    audit = list(state.get("audit", []))
    hypotheses = state.get("hypotheses", [])
    if not hypotheses:
        audit.append(
            audit_event(
                "Root Cause",
                "no_hypotheses",
                "nothing to test; confidence 0, escalating",
            )
        )
        return {
            "audit": audit,
            "root_cause": "",
            "root_cause_confidence": 0.0,
            "status": "escalated",
            "escalation_reason": "Diagnosis not yet implemented (Stage 2) "
            "or no hypotheses survived; a human gets the evidence trail.",
        }
    # Stage 2 replaces this pass through with real hypothesis testing.
    audit.append(audit_event("Root Cause", "testing_pending", "Stage 2"))
    return {"audit": audit}
