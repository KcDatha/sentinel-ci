"""Repair: draft the smallest safe fix, and prove it is allowed to exist.

Stage 2 drafts the patch; Stage 3 is what makes it trustworthy. The
Repair agent prompts Gemini for the smallest unified diff that fixes
the confirmed root cause, then runs the result through the
programmatic guard in :mod:`sentinel_ci.patch_guard` before the
patch enters the state at all:

* only files the evidence implicates may be touched,
* size caps keep "smallest possible" honest,
* a patch that weakens, skips, or deletes a test is rejected in
  code, whatever the model intended.

A rejected patch is discarded (never stored, never verified) and the
incident escalates with the guard's reasons in the audit trail. An
accepted patch stops the incident at ``awaiting_verification``: the
Stage 3 gate decides whether anyone may trust it.
"""

from __future__ import annotations

from ..config import Config
from ..llm import LLMError, complete
from ..patch_guard import validate_patch
from ..prompts import REPAIR_SYSTEM, repair_prompt
from ..state import SentinelCIState, audit_event


def allowed_files(state: SentinelCIState) -> set[str]:
    """The files a repair may touch: everything the evidence names.

    Changed files from the diff, files collected as code context, and
    the files holding the failing tests. Empty when the evidence pack
    has no file coordinates at all, in which case the guard's other
    rules (size, test weakening) still apply.
    """
    allowed: set[str] = set(state.get("changed_files", []))
    allowed.update(r.get("path", "") for r in state.get("code_context", []))
    allowed.update(t.split("::")[0] for t in state.get("failing_tests", []))
    return {p for p in allowed if p}


def _strip_fences(text: str) -> str:
    """Models fence diffs in markdown despite instructions; strip it."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[: -len("```")]
    return text.strip() + "\n"


def repair(state: SentinelCIState, config: Config | None = None) -> dict:
    config = config or Config.from_env()
    audit = list(state.get("audit", []))
    attempts = state.get("repair_attempts", 0) + 1
    updates: dict = {"audit": audit, "repair_attempts": attempts}

    if not state.get("root_cause"):
        updates["status"] = "escalated"
        updates["escalation_reason"] = (
            "Repair reached without a confirmed root cause; refusing to guess."
        )
        audit.append(audit_event("Repair", "escalated", updates["escalation_reason"]))
        return updates

    allowed = allowed_files(state)
    try:
        raw = complete(
            config,
            REPAIR_SYSTEM,
            repair_prompt(state, sorted(allowed)),
            max_tokens=4096,
        )
    except LLMError as exc:
        updates["status"] = "escalated"
        updates["escalation_reason"] = (
            f"Repair drafting unavailable ({str(exc)[:160]}); "
            "a human gets the evidence trail."
        )
        audit.append(audit_event("Repair", "escalated", updates["escalation_reason"]))
        return updates

    patch = _strip_fences(raw)
    verdict = validate_patch(patch, allowed_files=allowed or None)
    if not verdict.ok:
        reason = "; ".join(verdict.problems)
        updates["status"] = "escalated"
        updates["escalation_reason"] = (
            f"Repair guard rejected the proposed patch before verification: {reason}"
        )
        audit.append(audit_event("Repair", "patch_rejected_by_guard", reason))
        return updates

    updates["repair_patch"] = patch
    updates["repair_files"] = verdict.files
    updates["status"] = "awaiting_verification"
    audit.append(
        audit_event(
            "Repair",
            "patch_proposed",
            f"attempt {attempts}: minimal patch touching "
            f"{', '.join(verdict.files)}; held at the verification gate",
        )
    )
    return updates
