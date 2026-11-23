"""Adversarial Verification: try to break the fix before anyone trusts it.

Stage 3, and the heart of SENTINEL CI. Will check out the repaired
code in a clean Docker environment, rerun the originally failing tests
plus the regression suite, and argue against both the root cause and
the repair: wrong layer, masked symptom, new breakage elsewhere. The
verdict gates the pull request. Nothing is verified until the
challenge fails to break it.

Skeleton: with no patch to attack, the verdict is inconclusive, which
the graph treats as a failed gate: the fix does not ship and the
incident escalates once repair attempts are exhausted.
"""

from __future__ import annotations

from ..state import SentinelCIState, audit_event


def adversarial_verification(state: SentinelCIState) -> dict:
    audit = list(state.get("audit", []))
    if not state.get("repair_patch"):
        audit.append(
            audit_event(
                "Adversarial Verification",
                "verdict_inconclusive",
                "no patch to verify yet (Stage 3 runs the real gate)",
            )
        )
        return {
            "audit": audit,
            "verification_verdict": "inconclusive",
            "verification_notes": ["No repair patch present; gate cannot pass."],
        }
    # Stage 2 ends here by design: a patch exists, the gate has not
    # run, and no verdict is recorded. Stage 3 replaces this with the
    # Docker backed rerun and a real verdict.
    audit.append(
        audit_event(
            "Adversarial Verification",
            "gate_pending",
            "patch proposed; the verification gate itself is Stage 3 "
            "and has not run, so the fix is not trusted yet",
        )
    )
    return {"audit": audit}
