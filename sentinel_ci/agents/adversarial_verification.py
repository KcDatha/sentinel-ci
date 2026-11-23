"""Adversarial Verification: the gate a fix must survive, not pass once.

Stage 3, and the heart of SENTINEL CI. A proposed patch is verified
on three conditions, all of which must hold:

1. the originally failing tests pass with the patch applied,
2. the full test suite passes with no new failures relative to the
   failing run,
3. the Adversarial Verification agent, a Gemini call briefed to make
   the strongest case that the fix is wrong, incomplete, or only
   hiding the symptom, fails to break it.

Conditions 1 and 2 are behavioural: the patch is applied to a clean
checkout of the failing commit in an isolated sandbox (Docker when
available, else a temporary git worktree; see
:mod:`sentinel_ci.sandbox`) and the tests actually run. Condition 3
runs only when the tests are green: there is no point paying for an
attack on a patch the suite already killed, and the failure output
is the feedback the next repair attempt needs. A patch the adversary
breaks fails the gate even when every test is green, because a green
suite over a hardcoded or symptom masking fix is exactly the failure
mode this agent exists to catch.

The verdict contract with the rest of the graph:

* ``passed``: all three conditions held. The state moves to
  ``verified``; Stage 4 may open the pull request.
* ``failed``: at least one condition failed. The failing output or
  the adversary's break is recorded in ``verification_notes`` (and
  the ``adversary`` report), which the Repair agent's prompt reads
  on the next attempt; the Mission Controller routes back to Repair
  within the attempt budget, then escalates.
* ``inconclusive``: the gate itself could not reach a judgement (the
  adversary call failed, or its output was malformed). Inconclusive
  never ships and routes exactly like a failure.

When the incident carries no local checkout (``repo_path`` unset, as
in a logs only run), the gate cannot run at all. The agent records
``gate_pending`` and no verdict, and the incident stays held at
``awaiting_verification``: not trusted, not shipped, not bounced.
"""

from __future__ import annotations

from ..config import Config
from ..llm import LLMError, complete_json
from ..prompts import ADVERSARY_SYSTEM, adversary_prompt
from ..sandbox import SandboxResult, normalize_test_id, run_sandbox
from ..state import AdversaryReport, SentinelCIState, audit_event

AGENT = "Adversarial Verification"


def _validate_adversary(raw: dict) -> AdversaryReport:
    """Schema check one adversary response. Raises ValueError on a miss."""
    verdict = raw.get("verdict")
    if verdict not in {"survives", "broken"}:
        raise ValueError("adversary verdict is not survives or broken")
    attack = raw.get("attack")
    if not isinstance(attack, str) or not attack.strip():
        raise ValueError("adversary attack is missing")
    return {
        "verdict": verdict,
        "attack": attack.strip(),
        "reasoning": str(raw.get("reasoning", "")),
    }


def _sandbox_summary(state: SentinelCIState, result: SandboxResult,
                     targeted_failures: list[str],
                     new_failures: list[str]) -> str:
    """The test evidence the adversary is briefed with."""
    lines = [
        f"Sandbox mode: {result.mode}",
        "Originally failing tests still failing after the patch: "
        + (", ".join(targeted_failures) if targeted_failures else "none"),
        "New failures in the full suite (not failing in the incident run): "
        + (", ".join(new_failures) if new_failures else "none"),
        "Sandbox output tail:",
        result.output_tail,
    ]
    return "\n".join(lines)


def adversarial_verification(
    state: SentinelCIState, config: Config | None = None
) -> dict:
    config = config or Config.from_env()
    audit = list(state.get("audit", []))
    notes = list(state.get("verification_notes", []))
    attempt = state.get("repair_attempts", 0)
    updates: dict = {"audit": audit}

    if not state.get("repair_patch"):
        audit.append(
            audit_event(AGENT, "verdict_inconclusive", "no patch to verify")
        )
        notes.append("No repair patch present; gate cannot pass.")
        updates.update(
            verification_verdict="inconclusive", verification_notes=notes
        )
        return updates

    repo_path = state.get("repo_path") or ""
    if not repo_path:
        # The gate cannot run without a checkout to run against. The
        # incident stays held at the Stage 2 stop, exactly as before
        # Stage 3 existed: no verdict, nothing trusted.
        audit.append(
            audit_event(
                AGENT,
                "gate_pending",
                "patch proposed but the incident carries no local "
                "checkout, so the verification gate cannot run; the "
                "fix is held, not trusted",
            )
        )
        return updates

    updates["status"] = "verifying"
    result = run_sandbox(
        repo_path,
        state.get("head_sha", ""),
        state["repair_patch"],
        state.get("failing_tests", []),
        test_command=config.test_command,
        timeout=config.verify_timeout_seconds,
        image=config.sandbox_image,
    )
    updates["sandbox_mode"] = result.mode
    updates["verification_output"] = result.output_tail

    original = {normalize_test_id(t) for t in state.get("failing_tests", [])}

    def still_failing(run) -> list[str]:
        if run is None:
            return []
        return [
            tid for tid in run.failing_tests
            if normalize_test_id(tid) in original
        ]

    targeted_failures = still_failing(result.targeted)
    full_failures = (
        list(result.full.failing_tests) if result.full is not None else []
    )
    new_failures = [
        tid for tid in full_failures if normalize_test_id(tid) not in original
    ]
    updates["targeted_failures"] = targeted_failures
    updates["new_failures"] = new_failures

    # --- conditions 1 and 2: the behavioural gate ---------------------
    problems: list[str] = []
    if result.error:
        problems.append(f"the sandbox could not run: {result.error}")
    elif not result.patch_applied:
        problems.append(f"the patch did not apply: {result.patch_error}")
    else:
        if result.targeted is None or result.targeted.timed_out:
            problems.append("the targeted test run timed out")
        elif targeted_failures:
            problems.append(
                "originally failing tests still fail: "
                + ", ".join(targeted_failures)
            )
        elif not original and result.targeted.exit_code != 0:
            problems.append(
                "the targeted run failed without named failing tests "
                f"(exit code {result.targeted.exit_code})"
            )
        if result.full is None or result.full.timed_out:
            problems.append("the full suite run timed out")
        elif new_failures:
            problems.append(
                "the patch broke previously passing tests: "
                + ", ".join(new_failures)
            )

    if problems:
        for problem in problems:
            notes.append(f"attempt {attempt}: {problem}")
        updates["verification_notes"] = notes
        updates["verification_verdict"] = "failed"
        audit.append(
            audit_event(
                AGENT,
                "gate_failed",
                f"attempt {attempt} ({result.mode} sandbox): "
                + "; ".join(problems),
            )
        )
        return updates

    audit.append(
        audit_event(
            AGENT,
            "tests_green",
            f"attempt {attempt} ({result.mode} sandbox): originally "
            "failing tests pass and the full suite shows no new failures",
        )
    )

    # --- condition 3: the adversary ------------------------------------
    summary = _sandbox_summary(state, result, targeted_failures, new_failures)
    try:
        raw = complete_json(config, ADVERSARY_SYSTEM, adversary_prompt(state, summary))
        report = _validate_adversary(raw)
    except (LLMError, ValueError) as exc:
        # The gate could not reach a judgement. That is not a pass:
        # inconclusive routes back or escalates, never forward.
        notes.append(
            f"attempt {attempt}: adversary unavailable ({str(exc)[:160]}); "
            "gate inconclusive"
        )
        updates["verification_notes"] = notes
        updates["verification_verdict"] = "inconclusive"
        audit.append(
            audit_event(
                AGENT,
                "verdict_inconclusive",
                f"attempt {attempt}: adversary call unusable ({str(exc)[:160]})",
            )
        )
        return updates

    updates["adversary"] = report
    if report["verdict"] == "broken":
        notes.append(
            f"attempt {attempt}: adversary broke the fix: {report['attack']}"
        )
        updates["verification_notes"] = notes
        updates["verification_verdict"] = "failed"
        audit.append(
            audit_event(
                AGENT,
                "adversary_broke_fix",
                f"attempt {attempt}: {report['attack']}",
            )
        )
        return updates

    notes.append(
        f"attempt {attempt}: fix survived the adversary "
        f"(strongest attack: {report['attack']})"
    )
    updates["verification_notes"] = notes
    updates["verification_verdict"] = "passed"
    updates["status"] = "verified"
    audit.append(
        audit_event(
            AGENT,
            "gate_passed",
            f"attempt {attempt}: all three gate conditions held "
            f"({result.mode} sandbox); fix verified",
        )
    )
    return updates
