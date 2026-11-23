"""Codebase Analyst: maps the blast radius and seals the evidence pack.

Stage 2. Fetches the diff between the last green commit and the
failed run's head through the GitHub compare API, then collects the
code regions around the failure: the failing test files and the
source files the traceback frames point into, read at the failing
commit. Everything is bounded (diff size, file count, slice size)
and every bound that bites is recorded in the state, so diagnosis
knows when it is working from a partial picture.

When the incident has no repository coordinates (a local log run) or
the API cannot be reached, the analyst degrades honestly: it records
why the diff is missing and passes the parser evidence through,
rather than inventing context.
"""

from __future__ import annotations

import re

from ..config import Config
from ..github_client import GitHubClient, GitHubError
from ..state import CodeRegion, SentinelCIState, audit_event

MAX_DIFF_CHARS = 24_000
MAX_CONTEXT_FILES = 6
MAX_FILE_CHARS = 4_000
CONTEXT_LINES_AROUND_FRAME = 30

# Traceback frame shapes: pytest's "path/to/file.py:48: in test_x" and
# the interpreter's 'File "path/to/file.py", line 48, in test_x'.
_PYTEST_FRAME_RE = re.compile(r"^\s*(\S+\.py):(\d+):\s+in\s+\S+", re.MULTILINE)
_PY_FRAME_RE = re.compile(r'File "([^"]+\.py)", line (\d+)')


def _frame_files(state: SentinelCIState) -> list[tuple[str, int | None]]:
    """Files implicated by the failure, failing tests first.

    Order matters because the context cap keeps the head of the list:
    the failing test file, then the source files its frames point
    into, in first seen order.
    """
    ordered: list[tuple[str, int | None]] = []
    seen: set[str] = set()

    def add(path: str, line: int | None) -> None:
        if path and path not in seen:
            seen.add(path)
            ordered.append((path, line))

    for node_id in state.get("failing_tests", []):
        add(node_id.split("::")[0], None)
    for excerpt in state.get("error_excerpts", []):
        for path, line in _PYTEST_FRAME_RE.findall(excerpt):
            add(path, int(line))
        for path, line in _PY_FRAME_RE.findall(excerpt):
            add(path, int(line))
    return ordered


def _slice_around(text: str, line_no: int | None) -> tuple[str, str, bool]:
    """A bounded slice of one file: around a frame line when known.

    Returns (content, note, truncated)."""
    lines = text.splitlines()
    total = len(lines)
    if line_no is not None and 1 <= line_no <= total:
        start = max(0, line_no - 1 - CONTEXT_LINES_AROUND_FRAME)
        end = min(total, line_no + CONTEXT_LINES_AROUND_FRAME)
        content = "\n".join(lines[start:end])
        note = f"lines {start + 1}-{end} of {total}, around line {line_no}"
    else:
        content = "\n".join(lines)
        note = f"whole file, {total} lines"
    truncated = len(content) > MAX_FILE_CHARS
    if truncated:
        content = content[:MAX_FILE_CHARS]
        note += f"; capped at {MAX_FILE_CHARS} chars"
    return content, note, truncated


def codebase_analyst(
    state: SentinelCIState,
    config: Config | None = None,
    client: GitHubClient | None = None,
) -> dict:
    config = config or Config.from_env()
    audit = list(state.get("audit", []))
    repo = state.get("repo") or config.repo
    head_sha = state.get("head_sha")

    if not repo or not head_sha or repo == "local/log":
        audit.append(
            audit_event(
                "Codebase Analyst",
                "diff_unavailable",
                "no repository coordinates on the incident; "
                "diagnosis proceeds on the log evidence alone",
            )
        )
        return {"audit": audit}

    client = client or GitHubClient(config.github_token)
    try:
        base_sha = state.get("base_sha") or client.last_green_sha(
            repo, state.get("head_branch", ""), head_sha
        )
        if not base_sha:
            audit.append(
                audit_event(
                    "Codebase Analyst",
                    "diff_unavailable",
                    "no earlier green run found on this branch to diff against",
                )
            )
            return {"audit": audit}
        compare = client.compare_diff(repo, base_sha, head_sha)
    except GitHubError as exc:
        audit.append(
            audit_event("Codebase Analyst", "diff_fetch_failed", str(exc)[:200])
        )
        return {"audit": audit}

    updates: dict = {"audit": audit, "base_sha": base_sha}

    # --- the diff, bounded -------------------------------------------
    changed_files: list[str] = []
    diff_parts: list[str] = []
    for entry in compare.get("files", []):
        name = entry.get("filename", "")
        if not name:
            continue
        changed_files.append(name)
        patch = entry.get("patch")
        if patch:
            diff_parts.append(f"diff --git a/{name} b/{name}\n{patch}")
    diff_text = "\n".join(diff_parts)
    diff_truncated = len(diff_text) > MAX_DIFF_CHARS
    if diff_truncated:
        diff_text = diff_text[:MAX_DIFF_CHARS]
    updates["changed_files"] = changed_files
    updates["diff"] = diff_text
    updates["diff_truncated"] = diff_truncated
    audit.append(
        audit_event(
            "Codebase Analyst",
            "diff_collected",
            f"{len(changed_files)} changed file(s) between {base_sha[:8]} "
            f"and {head_sha[:8]}" + ("; diff truncated to cap" if diff_truncated else ""),
        )
    )

    # --- code regions around the failure, bounded ---------------------
    candidates = _frame_files(state)
    context_truncated = len(candidates) > MAX_CONTEXT_FILES
    regions: list[CodeRegion] = []
    for path, line_no in candidates[:MAX_CONTEXT_FILES]:
        try:
            text = client.get_file_text(repo, path, head_sha)
        except GitHubError:
            audit.append(
                audit_event(
                    "Codebase Analyst",
                    "context_file_unavailable",
                    f"{path} could not be read at {head_sha[:8]}",
                )
            )
            continue
        content, note, truncated = _slice_around(text, line_no)
        context_truncated = context_truncated or truncated
        regions.append({"path": path, "content": content, "note": note})
    updates["code_context"] = regions
    updates["context_truncated"] = context_truncated
    audit.append(
        audit_event(
            "Codebase Analyst",
            "evidence_pack_sealed",
            f"{len(regions)} code region(s) collected around the failure"
            + ("; context truncated to caps" if context_truncated else ""),
        )
    )
    return updates
