"""Root Cause: test hypotheses until one cause explains every symptom.

Stage 2. A second Gemini call plays skeptic against the top ranked
hypothesis: it must explain every failing test and error in the
evidence pack, with nothing left over and nothing contradicted, or
it is rejected. The graph routes on the outcome:

* confirmed at or above the confidence threshold: on to Repair,
* rejected with hypothesis attempts remaining: back to Failure
  Hypothesis, with the dead cause recorded so it is never re-proposed,
* rejected with attempts exhausted, confirmed below the threshold,
  or nothing to test: escalate to a human with the evidence trail.

A confident wrong answer is worse than an escalation, so every
uncertain path ends at a human, not at a patch.
"""

from __future__ import annotations

from ..config import Config
from ..llm import LLMError, complete_json
from ..prompts import ROOT_CAUSE_SYSTEM, root_cause_prompt
from ..state import SentinelCIState, audit_event


def _validate_verdict(raw: dict) -> dict:
    """Schema check one model response. Raises ValueError on any miss."""
    verdict = raw.get("verdict")
    if verdict not in {"confirmed", "rejected"}:
        raise ValueError("verdict is not confirmed or rejected")
    confidence = raw.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("confidence is not a number")
    return {
        "verdict": verdict,
        "root_cause": str(raw.get("root_cause", "")),
        "confidence": max(0.0, min(1.0, float(confidence))),
        "reasoning": str(raw.get("reasoning", "")),
    }


def _escalate(audit: list, reason: str) -> dict:
    audit.append(audit_event("Root Cause", "escalated", reason))
    return {
        "audit": audit,
        "status": "escalated",
        "escalation_reason": reason,
    }


def root_cause(state: SentinelCIState, config: Config | None = None) -> dict:
    config = config or Config.from_env()
    audit = list(state.get("audit", []))
    hypotheses = state.get("hypotheses", [])

    if not hypotheses:
        return _escalate(
            audit,
            "No hypotheses survived to test (diagnosis unavailable or "
            "malformed); a human gets the evidence trail.",
        )

    hypothesis = hypotheses[0]
    try:
        raw = complete_json(
            config, ROOT_CAUSE_SYSTEM, root_cause_prompt(state, hypothesis)
        )
        verdict = _validate_verdict(raw)
    except (LLMError, ValueError) as exc:
        return _escalate(
            audit,
            f"Root cause evaluation unavailable ({str(exc)[:160]}); "
            "a human gets the evidence trail.",
        )

    updates: dict = {"audit": audit}

    if verdict["verdict"] == "rejected":
        rejected = list(state.get("rejected_causes", []))
        rejected.append(hypothesis.get("cause", ""))
        updates["rejected_causes"] = rejected
        updates["root_cause_verdict"] = "rejected"
        updates["root_cause"] = ""
        updates["root_cause_confidence"] = 0.0
        attempts = state.get("hypothesis_attempts", 0)
        if attempts >= config.max_hypothesis_attempts:
            audit.append(
                audit_event(
                    "Root Cause",
                    "hypothesis_rejected",
                    f"rejected: {verdict['reasoning'][:200]}",
                )
            )
            updates.update(
                _escalate(
                    audit,
                    f"Hypothesis budget exhausted after {attempts} attempt(s); "
                    "no cause survived testing. A human gets the evidence trail.",
                )
            )
            updates["rejected_causes"] = rejected
            updates["root_cause_verdict"] = "rejected"
            return updates
        audit.append(
            audit_event(
                "Root Cause",
                "hypothesis_rejected",
                f"attempt {attempts} killed: {verdict['reasoning'][:200]}; "
                "routing back for a new hypothesis",
            )
        )
        return updates

    # verdict == "confirmed"
    if verdict["confidence"] < config.confidence_threshold:
        return _escalate(
            audit,
            f"Cause proposed but confidence {verdict['confidence']:.2f} is "
            f"below the {config.confidence_threshold:.2f} threshold; "
            "a human gets the evidence trail.",
        )

    updates["root_cause_verdict"] = "confirmed"
    updates["root_cause"] = verdict["root_cause"]
    updates["root_cause_confidence"] = verdict["confidence"]
    audit.append(
        audit_event(
            "Root Cause",
            "root_cause_confirmed",
            f"{verdict['root_cause']} (confidence {verdict['confidence']:.2f})",
        )
    )
    return updates
