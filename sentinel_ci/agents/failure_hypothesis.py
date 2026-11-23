"""Failure Hypothesis: ranked candidate causes, each with a falsifier.

Stage 2. Prompts Gemini (free tier) with the sealed evidence pack and
requires strict structured JSON back: plain language hypotheses, the
evidence supporting each, a falsifier, and a calibrated confidence
with a note. The response is schema validated in code; a malformed
response produces no hypotheses, and the incident escalates at the
Root Cause step rather than guessing forward. Hypotheses are always
written before any code changes, and causes the Root Cause agent has
already rejected are fed back as forbidden, so a retry must think
something new.
"""

from __future__ import annotations

from ..config import Config
from ..llm import LLMError, complete_json
from ..prompts import HYPOTHESIS_SYSTEM, hypothesis_prompt
from ..state import Hypothesis, SentinelCIState, audit_event

MAX_HYPOTHESES = 3


def _validate_hypotheses(raw: dict) -> list[Hypothesis]:
    """Schema check one model response. Raises ValueError on any miss."""
    items = raw.get("hypotheses")
    if not isinstance(items, list) or not items:
        raise ValueError("response has no hypotheses list")
    out: list[Hypothesis] = []
    for item in items[:MAX_HYPOTHESES]:
        if not isinstance(item, dict):
            raise ValueError("hypothesis is not an object")
        cause = item.get("cause")
        evidence = item.get("supporting_evidence")
        confidence = item.get("confidence")
        if not isinstance(cause, str) or not cause.strip():
            raise ValueError("hypothesis cause is missing")
        if not isinstance(evidence, list) or not all(
            isinstance(e, str) for e in evidence
        ):
            raise ValueError("hypothesis supporting_evidence is not a string list")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            raise ValueError("hypothesis confidence is not a number")
        out.append(
            {
                "cause": cause.strip(),
                "supporting_evidence": list(evidence),
                "falsifier": str(item.get("falsifier", "")),
                "confidence": max(0.0, min(1.0, float(confidence))),
                "confidence_note": str(item.get("confidence_note", "")),
            }
        )
    return out


def failure_hypothesis(
    state: SentinelCIState, config: Config | None = None
) -> dict:
    config = config or Config.from_env()
    audit = list(state.get("audit", []))
    attempts = state.get("hypothesis_attempts", 0) + 1
    updates: dict = {"audit": audit, "hypothesis_attempts": attempts}

    try:
        raw = complete_json(config, HYPOTHESIS_SYSTEM, hypothesis_prompt(state))
        hypotheses = _validate_hypotheses(raw)
    except (LLMError, ValueError) as exc:
        audit.append(
            audit_event(
                "Failure Hypothesis",
                "diagnosis_unavailable",
                f"attempt {attempts}: no usable hypotheses ({str(exc)[:160]})",
            )
        )
        updates["hypotheses"] = []
        return updates

    updates["hypotheses"] = hypotheses
    top = hypotheses[0]
    audit.append(
        audit_event(
            "Failure Hypothesis",
            "hypotheses_proposed",
            f"attempt {attempts}: {len(hypotheses)} candidate(s); top: "
            f"{top['cause']} (confidence {top['confidence']:.2f})",
        )
    )
    return updates
