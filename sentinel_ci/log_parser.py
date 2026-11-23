"""Stage 1: turn raw GitHub Actions log text into a compact failure summary.

The parser is deliberately framework free (pure stdlib) and tolerant:
real Actions logs are timestamp prefixed, interleaved with setup noise,
and end with runner annotations. The goal is not to understand every
build system; it is to extract the small set of facts the diagnosis
agents need:

* which tests failed (pytest today; the patterns are easy to extend),
* the error excerpt around each failure (the traceback tail, bounded),
* the process exit code and any ``##[error]`` runner annotations,
* a one line summary a human or an LLM can act on.

Everything extracted is verbatim from the log. Nothing is inferred
beyond grouping and trimming.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# GitHub prefixes most log lines with an ISO timestamp: 2026-11-22T04:12:03.1234567Z
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s?")
_ERROR_ANNOTATION_RE = re.compile(r"^##\[error\](.*)$")
_EXIT_CODE_RE = re.compile(r"Process completed with exit code (\d+)")
_SECTION_RE = re.compile(r"^={3,}\s*(.*?)\s*={3,}$")
_FAILURE_BLOCK_RE = re.compile(r"^_{3,}\s*(.+?)\s*_{3,}$")
_PYTEST_FAILED_RE = re.compile(r"^FAILED\s+(\S+)")
_PYTEST_ERROR_RE = re.compile(r"^ERROR\s+(\S+)")
_PYTEST_INLINE_RE = re.compile(r"^(\S+::\S+)\s+(FAILED|ERROR)\b")
_GENERIC_ERROR_RE = re.compile(
    r"(error:|Error:|ERROR:|Traceback \(most recent call last\)|fatal:|FAILURE|BUILD FAILED)"
)

MAX_EXCERPT_LINES = 40
MAX_EXCERPTS = 10
MAX_GENERIC_ERROR_LINES = 20


@dataclass
class FailureSummary:
    """The compact, structured result of parsing one run's logs."""

    failing_tests: list[str] = field(default_factory=list)
    error_excerpts: list[str] = field(default_factory=list)
    annotations: list[str] = field(default_factory=list)
    exit_code: int | None = None
    summary_line: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def strip_timestamps(text: str) -> list[str]:
    """Split raw log text into lines with runner timestamps removed."""
    return [_TIMESTAMP_RE.sub("", line).rstrip() for line in text.splitlines()]


def _pytest_failures(lines: list[str]) -> list[str]:
    """Failing test node ids, from the short summary and inline reports."""
    found: list[str] = []
    for line in lines:
        for pattern in (_PYTEST_FAILED_RE, _PYTEST_ERROR_RE):
            m = pattern.match(line)
            if m and m.group(1) not in found:
                found.append(m.group(1))
        m = _PYTEST_INLINE_RE.match(line)
        if m and m.group(1) not in found:
            found.append(m.group(1))
    return found


def _failure_block_excerpts(lines: list[str]) -> list[str]:
    """Traceback tails from the pytest FAILURES and ERRORS sections.

    Each block is headed by an underscore wrapped test name. The tail of
    the block carries the assertion and the ``E`` lines, which is what a
    diagnosis needs; the head is mostly source context, so excerpts keep
    the last MAX_EXCERPT_LINES lines of each block.
    """
    excerpts: list[str] = []
    in_failures = False
    block: list[str] = []

    def flush() -> None:
        if block:
            excerpts.append("\n".join(block[-MAX_EXCERPT_LINES:]))

    for line in lines:
        section = _SECTION_RE.match(line)
        if section:
            flush()
            block = []
            in_failures = section.group(1).upper() in {"FAILURES", "ERRORS"}
            continue
        if in_failures:
            if _FAILURE_BLOCK_RE.match(line):
                flush()
                block = [line]
            elif block:
                block.append(line)
    flush()
    return excerpts[:MAX_EXCERPTS]


def _generic_error_excerpts(lines: list[str]) -> list[str]:
    """For non test failures: the lines that look like the actual error."""
    hits = [line for line in lines if _GENERIC_ERROR_RE.search(line)]
    return hits[:MAX_GENERIC_ERROR_LINES]


def parse_log_text(text: str) -> FailureSummary:
    """Parse raw Actions log text into a :class:`FailureSummary`."""
    lines = strip_timestamps(text)

    exit_code: int | None = None
    annotations: list[str] = []
    for line in lines:
        m = _EXIT_CODE_RE.search(line)
        if m:
            exit_code = int(m.group(1))
        a = _ERROR_ANNOTATION_RE.match(line)
        if a and a.group(1).strip():
            annotations.append(a.group(1).strip())

    failing_tests = _pytest_failures(lines)
    excerpts = _failure_block_excerpts(lines)
    if not excerpts:
        excerpts = _generic_error_excerpts(lines)

    if failing_tests:
        named = ", ".join(failing_tests[:3])
        extra = f" and {len(failing_tests) - 3} more" if len(failing_tests) > 3 else ""
        summary = f"{len(failing_tests)} failing test(s): {named}{extra}"
        if exit_code is not None:
            summary += f"; exit code {exit_code}"
    elif exit_code is not None:
        summary = f"Run failed with exit code {exit_code}; no test failures detected"
    elif annotations:
        summary = f"Run failed: {annotations[0]}"
    else:
        summary = "Run failed; no structured failure markers found in log"

    return FailureSummary(
        failing_tests=failing_tests,
        error_excerpts=excerpts,
        annotations=annotations,
        exit_code=exit_code,
        summary_line=summary,
    )


def parse_log_file(path: str) -> FailureSummary:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return parse_log_text(fh.read())
