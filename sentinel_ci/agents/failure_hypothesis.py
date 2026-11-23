"""Failure Hypothesis: ranked candidate causes, each with a falsifier.

Stage 2. Will prompt the model with the failure summary, the error
excerpts, and the diff, and require every hypothesis to name the
observation that would confirm or kill it, per the SENTINEL pattern.

Skeleton: produces no hypotheses. With nothing to test, the Root Cause
agent reports zero confidence and the controller escalates to a human,
which is the honest behaviour for an unimplemented diagnosis stage.
"""

from __future__ import annotations

from ..state import SentinelCIState, audit_event


def failure_hypothesis(state: SentinelCIState) -> dict:
    audit = list(state.get("audit", []))
    audit.append(
        audit_event(
            "Failure Hypothesis",
            "hypotheses_pending",
            "LLM diagnosis arrives in Stage 2",
        )
    )
    return {"audit": audit, "hypotheses": state.get("hypotheses", [])}
