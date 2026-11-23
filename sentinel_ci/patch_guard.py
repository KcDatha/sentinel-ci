"""Programmatic guard for Repair agent patches.

The Repair agent's prompt carries the rules (smallest fix, no
unrelated files, never weaken a test), but a prompt is a request, not
a guarantee. This module enforces the load bearing rules in code, on
the raw unified diff, before the patch is allowed anywhere near the
verification gate:

* the patch must parse as a unified diff,
* it must stay inside the incident's allowed file set (the files the
  evidence actually implicates) when that set is known,
* it must stay small (file count and changed line caps),
* it must not weaken, skip, or delete a test: no deleted test files,
  no removed test functions, no net loss of assertions in a test
  file, and no skip or xfail markers added anywhere.

A rejected patch is discarded by the Repair agent and the incident
escalates with the guard's reasons in the audit trail. Stage 3's
verification gate is the behavioural check; this guard is the
structural one that runs first and costs nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_PATCH_FILES = 5
MAX_CHANGED_LINES = 250

_GIT_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")
_SKIP_MARKER_RE = re.compile(
    r"(pytest\.mark\.skip|pytest\.mark\.xfail|pytest\.skip\(|unittest\.skip|"
    r"skipTest\(|@skip\b|it\.skip\(|xit\(|xdescribe\(|\.skip\()"
)
_TEST_DEF_RE = re.compile(r"^\s*(async\s+)?def\s+(test_\w+)\s*\(")
_ASSERT_RE = re.compile(r"\bassert\b")


@dataclass
class FilePatch:
    path: str  # the b/ side path (new name)
    old_path: str = ""
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    deleted: bool = False


@dataclass
class PatchVerdict:
    ok: bool
    files: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def is_test_path(path: str) -> bool:
    """Heuristic: does this path name a test file (python and js)?"""
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    if any(part in {"test", "tests", "__tests__"} for part in parts[:-1]):
        return True
    if base.startswith("test_") or base.endswith("_test.py"):
        return True
    if base.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return True
    return base in {"conftest.py"}


def parse_unified_diff(text: str) -> list[FilePatch]:
    """Split a unified diff into per file added and removed lines.

    Tolerant of a diff without ``diff --git`` headers (plain
    ``---``/``+++`` pairs). Lines outside hunks are ignored except
    headers. Returns an empty list when nothing parses as a diff.
    """
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    for raw in text.splitlines():
        header = _GIT_HEADER_RE.match(raw)
        if header:
            current = FilePatch(path=header.group(2), old_path=header.group(1))
            patches.append(current)
            continue
        if raw.startswith("--- "):
            if current is None or current.added or current.removed:
                current = FilePatch(path="", old_path=raw[4:].strip().removeprefix("a/"))
                patches.append(current)
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if current is None:
                current = FilePatch(path="")
                patches.append(current)
            if target == "/dev/null":
                current.deleted = True
            else:
                current.path = target.removeprefix("b/")
            continue
        if current is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            current.added.append(raw[1:])
        elif raw.startswith("-") and not raw.startswith("---"):
            current.removed.append(raw[1:])
    return [p for p in patches if p.path or p.deleted]


def validate_patch(
    patch_text: str,
    allowed_files: set[str] | None = None,
    max_files: int = MAX_PATCH_FILES,
    max_changed_lines: int = MAX_CHANGED_LINES,
) -> PatchVerdict:
    """Check one proposed patch against the guard rules."""
    problems: list[str] = []
    if not patch_text or not patch_text.strip():
        return PatchVerdict(ok=False, problems=["patch is empty"])
    file_patches = parse_unified_diff(patch_text)
    if not file_patches:
        return PatchVerdict(ok=False, problems=["patch is not a parseable unified diff"])

    files = [p.path for p in file_patches if p.path]
    if len(file_patches) > max_files:
        problems.append(
            f"patch touches {len(file_patches)} files, over the cap of {max_files}"
        )
    changed = sum(len(p.added) + len(p.removed) for p in file_patches)
    if changed > max_changed_lines:
        problems.append(
            f"patch changes {changed} lines, over the cap of {max_changed_lines}"
        )

    if allowed_files:
        for path in files:
            if path not in allowed_files:
                problems.append(f"patch touches unrelated file outside the evidence: {path}")

    for fp in file_patches:
        # Skip markers anywhere in the patch are a symptom fix.
        for line in fp.added:
            if _SKIP_MARKER_RE.search(line):
                problems.append(f"patch adds a skip/xfail marker in {fp.path or fp.old_path}")
                break
        if not is_test_path(fp.path or fp.old_path):
            continue
        if fp.deleted:
            problems.append(f"patch deletes test file {fp.old_path or fp.path}")
            continue
        removed_tests = [ln for ln in fp.removed if _TEST_DEF_RE.match(ln)]
        added_tests = [ln for ln in fp.added if _TEST_DEF_RE.match(ln)]
        if len(removed_tests) > len(added_tests):
            problems.append(
                f"patch removes test function(s) from {fp.path}: "
                + ", ".join(m.group(2) for m in map(_TEST_DEF_RE.match, removed_tests) if m)
            )
        removed_asserts = sum(1 for ln in fp.removed if _ASSERT_RE.search(ln))
        added_asserts = sum(1 for ln in fp.added if _ASSERT_RE.search(ln))
        if removed_asserts > added_asserts:
            problems.append(
                f"patch removes {removed_asserts - added_asserts} assertion(s) "
                f"from {fp.path} without replacement"
            )

    return PatchVerdict(ok=not problems, files=files, problems=problems)
