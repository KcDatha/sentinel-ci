"""Stage 4: publish a verified fix as a pull request.

The Mission Controller owns this stage, per the SENTINEL pattern: it
runs only on an incident whose status is ``verified`` (all three
Stage 3 gate conditions held in the same attempt), and it never runs
on anything else. The patch is published through the GitHub Git data
API (blob, tree, commit, ref), so no local clone is needed here: the
branch ``sentinel-ci/run-<run_id>`` is created at the failing commit,
the verified patch is applied to the file contents fetched at that
commit, and the pull request opens against the branch the failed run
was on.

The pull request body is the audit trail, written for the human who
has to decide whether to merge: the failure summary, the hypothesis
and the confirmed root cause, the diff itself, the three verification
gate results (including the adversary's strongest attack and the fact
that it failed to break the fix), and the attempt history. SENTINEL
CI never merges; merging stays a human decision, and the rollback
path is closing the pull request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .github_client import GitHubClient, GitHubError
from .state import SentinelCIState, audit_event

AGENT = "Mission Controller"

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PullRequestError(RuntimeError):
    """The fix could not be published; the incident stays verified."""


class PatchApplyError(PullRequestError):
    """The verified patch did not apply to the fetched file contents."""


# -- unified diff parsing and application -------------------------------
# The Repair agent emits standard unified diffs (the Stage 2 guard
# already proved the patch parses and stays inside the evidence).
# Applying one needs hunk positions, which the guard's summary parse
# does not keep, so this module parses hunks itself and applies them
# strictly: context and removed lines must match the fetched file at
# the stated position, or the stage refuses rather than guessing.


@dataclass
class Hunk:
    old_start: int  # 1 based line number in the old file
    old_count: int = 0  # old lines the hunk spans; 0 means pure insertion
    lines: list[tuple[str, str]] = field(default_factory=list)  # (kind, text)


@dataclass
class FileDiff:
    old_path: str  # "" when the patch creates the file
    new_path: str  # "" when the patch deletes the file
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path


def parse_diff_hunks(patch_text: str) -> list[FileDiff]:
    """Split a unified diff into per file hunks. Raises on no diff."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    for raw in patch_text.splitlines():
        if raw.startswith("diff --git "):
            current = FileDiff(old_path="", new_path="")
            files.append(current)
            hunk = None
            continue
        if raw.startswith("--- "):
            if current is None or current.hunks:
                current = FileDiff(old_path="", new_path="")
                files.append(current)
            target = raw[4:].strip()
            current.old_path = "" if target == "/dev/null" else target.removeprefix("a/")
            hunk = None
            continue
        if raw.startswith("+++ "):
            if current is None:
                current = FileDiff(old_path="", new_path="")
                files.append(current)
            target = raw[4:].strip()
            current.new_path = "" if target == "/dev/null" else target.removeprefix("b/")
            hunk = None
            continue
        match = _HUNK_RE.match(raw)
        if match and current is not None:
            hunk = Hunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2)) if match.group(2) is not None else 1,
            )
            current.hunks.append(hunk)
            continue
        if hunk is not None and raw[:1] in {" ", "-", "+", "\\"}:
            if raw.startswith("\\"):
                continue  # "\ No newline at end of file" marker
            hunk.lines.append((raw[0], raw[1:]))
    files = [f for f in files if f.hunks or f.old_path or f.new_path]
    if not files:
        raise PatchApplyError("patch contains no file diffs")
    return files


def apply_file_diff(old_text: str, file_diff: FileDiff) -> str:
    """Apply one file's hunks to its old contents; strict matching.

    Returns the new contents. A deleted file returns "" and the
    caller drops it from the tree instead of storing the empty text.
    """
    if file_diff.old_path and not file_diff.new_path:
        return ""  # file deleted by the patch
    lines = old_text.split("\n")
    # A trailing newline shows up as a final empty element; work on
    # real lines and restore it at the end.
    trailing_newline = bool(lines) and lines[-1] == ""
    if trailing_newline:
        lines = lines[:-1]
    offset = 0
    for hunk in file_diff.hunks:
        # A hunk spanning old lines starts at old_start (1 based); a
        # pure insertion hunk (-N,0) inserts after old line N.
        if hunk.old_count == 0:
            idx = hunk.old_start + offset
        else:
            idx = hunk.old_start - 1 + offset
        if idx < 0 or idx > len(lines):
            raise PatchApplyError(
                f"hunk for {file_diff.path} starts past the end of the file"
            )
        consumed = 0
        produced: list[str] = []
        for kind, text in hunk.lines:
            if kind == " ":
                if idx + consumed >= len(lines) or lines[idx + consumed] != text:
                    raise PatchApplyError(
                        f"context mismatch in {file_diff.path} "
                        f"near line {hunk.old_start + consumed}"
                    )
                produced.append(text)
                consumed += 1
            elif kind == "-":
                if idx + consumed >= len(lines) or lines[idx + consumed] != text:
                    raise PatchApplyError(
                        f"removed line mismatch in {file_diff.path} "
                        f"near line {hunk.old_start + consumed}"
                    )
                consumed += 1
            else:  # "+"
                produced.append(text)
        lines[idx:idx + consumed] = produced
        offset += len(produced) - consumed
    out = "\n".join(lines)
    if trailing_newline or not file_diff.old_path:
        out += "\n"
    return out


# -- pull request title and body ------------------------------------------

_MAX_TITLE_TESTS = 3


def _short_test_name(node_id: str) -> str:
    return node_id.split("::")[-1] if node_id else node_id


def build_pr_title(state: SentinelCIState) -> str:
    """``fix: <failing test names>``, short names only, capped length."""
    names = [_short_test_name(t) for t in state.get("failing_tests", []) if t]
    if not names:
        return "fix: failing CI run"
    shown = names[:_MAX_TITLE_TESTS]
    title = "fix: " + ", ".join(shown)
    extra = len(names) - len(shown)
    if extra > 0:
        title += f" (+{extra} more)"
    return title[:72]


def _gate_section(state: SentinelCIState) -> list[str]:
    targeted = state.get("targeted_failures", [])
    new_failures = state.get("new_failures", [])
    adversary = state.get("adversary", {}) or {}
    mode = state.get("sandbox_mode", "") or "sandbox"
    lines = [
        "## Verification gate",
        "",
        "All three conditions held in the same attempt before this "
        "pull request opened:",
        "",
        "1. **Originally failing tests pass.** The targeted rerun in "
        f"a clean {mode} sandbox shows none of the originally failing "
        "tests still failing"
        + ("." if not targeted else f" (still failing: {', '.join(targeted)})."),
        "2. **No new failures.** The full suite run shows no failures "
        "the failing run did not already have"
        + ("." if not new_failures else f" (new: {', '.join(new_failures)})."),
        "3. **The adversary failed to break the fix.** The Adversarial "
        "Verification agent was briefed to make the strongest case "
        "that the fix is wrong, incomplete, or only hiding the "
        "symptom, and its verdict was "
        f"`{adversary.get('verdict', 'survives')}`.",
    ]
    if adversary.get("attack"):
        lines += [
            "",
            f"Its strongest attack was: {adversary['attack']}",
        ]
    if adversary.get("reasoning"):
        lines.append(f"Why the fix survived: {adversary['reasoning']}")
    return lines


def build_pr_body(state: SentinelCIState) -> str:
    """The audit trail as a pull request body."""
    repo = state.get("repo", "")
    run_id = state.get("run_id", "")
    lines: list[str] = [
        "## Failure",
        "",
        state.get("failure_summary", "") or "A workflow run failed.",
        "",
        f"Repository: {repo} · Failed run: {run_id} · "
        f"Workflow: {state.get('workflow_name', '')} · "
        f"Branch: {state.get('head_branch', '')} · "
        f"Failing commit: `{state.get('head_sha', '')}`",
        "",
    ]
    failing = state.get("failing_tests", [])
    if failing:
        lines += ["Failing tests:", ""]
        lines += [f"- `{t}`" for t in failing]
        lines.append("")
    excerpts = state.get("error_excerpts", [])
    if excerpts:
        lines += ["Error excerpt:", "", "```", excerpts[0][:1200], "```", ""]

    lines += ["## Root cause", ""]
    hypotheses = state.get("hypotheses", [])
    if hypotheses:
        top = hypotheses[0]
        lines.append(f"Hypothesis: {top.get('cause', '')}")
        if top.get("confidence_note"):
            lines.append(f"Confidence note: {top['confidence_note']}")
        lines.append("")
    lines.append(f"Confirmed root cause: {state.get('root_cause', '')}")
    lines.append(
        f"Root cause confidence: {state.get('root_cause_confidence', 0.0):.2f}"
    )
    lines.append("")

    lines += [
        "## The fix",
        "",
        "The smallest change the Repair agent could make for that "
        "cause, checked by the patch guard before it existed:",
        "",
        "```diff",
        (state.get("repair_patch", "") or "").rstrip(),
        "```",
        "",
    ]

    lines += _gate_section(state)
    lines.append("")

    lines += [
        "## Attempt history",
        "",
        f"- Hypothesis attempts: {state.get('hypothesis_attempts', 0)}"
        + (
            f" (rejected on the way: {'; '.join(state['rejected_causes'])})"
            if state.get("rejected_causes")
            else ""
        ),
        f"- Repair attempts: {state.get('repair_attempts', 0)}",
    ]
    notes = state.get("verification_notes", [])
    if notes:
        lines.append("- Verification notes:")
        lines += [f"  - {n}" for n in notes]
    lines.append("")

    audit = state.get("audit", [])
    if audit:
        lines += ["## Audit trail", ""]
        lines += [
            f"- **{e.get('agent', '')}** · {e.get('action', '')}"
            + (f" — {e['detail']}" if e.get("detail") else "")
            for e in audit
        ]
        lines.append("")

    lines += [
        "---",
        "Opened by SENTINEL CI from a verified fix. SENTINEL CI never "
        "auto merges: review and merge are yours. Rollback is closing "
        "this pull request; nothing else was changed.",
    ]
    return "\n".join(lines)


# -- the stage itself -------------------------------------------------------


def branch_name(state: SentinelCIState) -> str:
    return f"sentinel-ci/run-{state.get('run_id', 'unknown')}"


def open_pull_request(state: SentinelCIState, client: GitHubClient) -> dict:
    """Publish the verified patch; returns state updates.

    Raises PullRequestError when the incident is not verified or the
    patch cannot be published. The caller decides what a failure
    means (the pipeline records it in the audit trail and leaves the
    incident verified: the fix is proven, the publishing failed).
    """
    if state.get("status") != "verified" or state.get("verification_verdict") != "passed":
        raise PullRequestError(
            "pull requests open only from a verified fix; incident "
            f"status is {state.get('status')!r} with gate verdict "
            f"{state.get('verification_verdict')!r}"
        )
    patch = state.get("repair_patch", "") or ""
    if not patch.strip():
        raise PullRequestError("verified incident carries no repair patch")
    repo = state.get("repo", "")
    head_sha = state.get("head_sha", "")
    if not repo or not head_sha:
        raise PullRequestError("incident is missing its repo or failing commit")

    audit = list(state.get("audit", []))
    file_diffs = parse_diff_hunks(patch)
    try:
        base_commit = client.get_commit(repo, head_sha)
        base_tree = base_commit["tree"]["sha"]
        entries: list[dict] = []
        for fd in file_diffs:
            if fd.old_path and not fd.new_path:
                entries.append(
                    {"path": fd.old_path, "mode": "100644",
                     "type": "blob", "sha": None}
                )
                continue
            old_text = (
                client.get_file_text(repo, fd.old_path, head_sha)
                if fd.old_path
                else ""
            )
            new_text = apply_file_diff(old_text, fd)
            blob_sha = client.create_blob(repo, new_text)
            entries.append(
                {"path": fd.new_path, "mode": "100644",
                 "type": "blob", "sha": blob_sha}
            )
        tree_sha = client.create_tree(repo, base_tree, entries)
        title = build_pr_title(state)
        commit_sha = client.create_commit(repo, title, tree_sha, [head_sha])
        branch = branch_name(state)
        client.create_ref(repo, f"heads/{branch}", commit_sha)
        base_branch = state.get("head_branch", "")
        if not base_branch:
            base_branch = client.get_repo(repo).get("default_branch", "main")
        # Mid range confidence opens as a draft (BUILD_PLAN Stage 4):
        # the gate passed, but a human should look first either way.
        draft = state.get("root_cause_confidence", 1.0) < 0.85
        pr = client.create_pull_request(
            repo, title, build_pr_body(state), branch, base_branch, draft=draft
        )
    except GitHubError as exc:
        raise PullRequestError(f"GitHub API failed while publishing: {exc}") from exc

    pr_url = pr.get("html_url", "")
    audit.append(
        audit_event(
            AGENT,
            "pr_opened",
            f"{pr_url} on branch {branch} (base {base_branch}"
            + (", draft" if draft else "")
            + "); awaiting human review, never auto merged",
        )
    )
    return {"pr_url": pr_url, "status": "ready_for_pr", "audit": audit}
