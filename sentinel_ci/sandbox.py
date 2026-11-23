"""Stage 3 sandbox: run a proposed patch against the real test suite.

The verification gate does not trust claims about a patch; it runs
the patch. This module provides the isolated execution:

* the incident repository is checked out at the failing commit into
  a throwaway location (a ``git worktree`` of the operator's local
  checkout), the proposed patch is applied there, and the project
  tests run in that tree. The original checkout is never mutated:
  the worktree is removed when the run ends, pass or fail.
* when a ``docker`` binary is available, the test commands execute
  inside a container with the patched worktree mounted, so the run
  is isolated from the host environment as well as from the original
  checkout. Without Docker, the same worktree runs the tests with the
  host interpreter (the fallback this project's own test suite
  exercises). Which mode ran is recorded in the result, never
  assumed.

Two runs happen per gate attempt: a targeted run over the files
holding the originally failing tests, and a full suite run. Their
outputs are parsed back into failing test ids (pytest output through
the Stage 1 parser, unittest output through a small dedicated
parser) and normalised to ``(file, test name)`` pairs so results
compare cleanly against the Stage 1 evidence whatever runner the
project uses.

Every subprocess is bounded by a timeout; a run that exceeds it is
reported as timed out, which the gate treats as a failed condition,
never as a pass.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

from .log_parser import parse_log_text

MAX_OUTPUT_CHARS = 8_000

_UNITTEST_FAILURE_RE = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([\w.]+)\)", re.MULTILINE)


@dataclass
class TestRun:
    """One executed test command and what it proved."""

    command: list[str]
    exit_code: int | None  # None when the run timed out
    output: str  # bounded tail
    failing_tests: list[str]  # node ids in the runner's own format
    timed_out: bool = False


@dataclass
class SandboxResult:
    """Everything one gate attempt learned from the sandbox."""

    mode: str  # "docker" | "worktree"
    patch_applied: bool = False
    patch_error: str = ""
    error: str = ""  # sandbox itself could not run (no repo, bad sha)
    targeted: TestRun | None = None
    full: TestRun | None = None

    @property
    def output_tail(self) -> str:
        """The combined, bounded output a human or an agent reads."""
        parts: list[str] = []
        if self.error:
            parts.append(f"sandbox error: {self.error}")
        if not self.patch_applied and self.patch_error:
            parts.append(f"patch did not apply: {self.patch_error}")
        for label, run in (("targeted", self.targeted), ("full suite", self.full)):
            if run is None:
                continue
            status = "timed out" if run.timed_out else f"exit code {run.exit_code}"
            parts.append(f"--- {label} run ({status}): {shlex.join(run.command)}")
            parts.append(run.output)
        return "\n".join(parts)[-MAX_OUTPUT_CHARS:]


def docker_available() -> bool:
    return shutil.which("docker") is not None


def normalize_test_id(node_id: str) -> tuple[str, str]:
    """``(file, test name)`` for a node id from any supported runner.

    pytest plain functions report ``path::test_name``; pytest over
    unittest classes reports ``path::Class::test_name``; the unittest
    parser below already emits ``path::test_name``. The file plus the
    final segment is the stable identity across all three.
    """
    parts = node_id.split("::")
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return node_id, node_id


def _parse_unittest_failures(output: str) -> list[str]:
    """Failing ids from unittest output, as ``path::test_name``.

    Python 3.12 headers read ``FAIL: test_x (pkg.mod.Class.test_x)``;
    older versions stop at the class. Either way the file is the
    dotted path minus the method segment (already captured) and the
    TestCase class segment.
    """
    found: list[str] = []
    for name, dotted in _UNITTEST_FAILURE_RE.findall(output):
        parts = dotted.split(".")
        if parts and parts[-1] == name:
            parts = parts[:-1]  # the method segment
        if parts and parts[-1][:1].isupper():
            parts = parts[:-1]  # the TestCase class segment
        node_id = "/".join(parts) + ".py::" + name
        if node_id not in found:
            found.append(node_id)
    return found


def _parse_failures(output: str, command: list[str]) -> list[str]:
    if any("unittest" in token for token in command):
        return _parse_unittest_failures(output)
    return parse_log_text(output).failing_tests


def detect_test_command(repo_dir: str) -> str:
    """Pick the project's test command when none was configured.

    pytest when the project declares it or it is installed for the
    current interpreter; otherwise unittest discovery over a tests
    directory; otherwise pytest as the documented default.
    """
    for name in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini",
                 "requirements.txt", "requirements-dev.txt"):
        try:
            with open(os.path.join(repo_dir, name), encoding="utf-8",
                      errors="replace") as fh:
                if "pytest" in fh.read():
                    return f"{sys.executable} -m pytest -q"
        except OSError:
            continue
    import importlib.util

    if importlib.util.find_spec("pytest") is not None:
        return f"{sys.executable} -m pytest -q"
    if os.path.isdir(os.path.join(repo_dir, "tests")):
        return f"{sys.executable} -m unittest discover -s tests -t ."
    return f"{sys.executable} -m pytest -q"


def _test_commands(base: list[str], failing_tests: list[str]) -> tuple[list[str], list[str]]:
    """(targeted, full) command lines for one gate attempt.

    The targeted run is scoped to the files holding the originally
    failing tests (module form for unittest, path form for pytest);
    the full run is the base command untouched.
    """
    files = sorted({t.split("::")[0] for t in failing_tests if t})
    joined = " ".join(base)
    if "unittest" in joined and "discover" in joined:
        if files:
            modules = [f[:-3].replace("/", ".") for f in files if f.endswith(".py")]
            if modules:
                python = base[0] if base else sys.executable
                return [python, "-m", "unittest", *modules], list(base)
        return list(base), list(base)
    if "pytest" in joined and files:
        return list(base) + files, list(base)
    return list(base), list(base)


def _run(command: list[str], cwd: str, timeout: int, mode: str,
         image: str) -> TestRun:
    """Execute one test command, in Docker when that is the mode."""
    if mode == "docker":
        inner = list(command)
        if inner and inner[0] == sys.executable:
            inner[0] = "python3"
        argv = [
            "docker", "run", "--rm",
            "-v", f"{cwd}:/work", "-w", "/work",
            image, "sh", "-c", shlex.join(inner),
        ]
    else:
        argv = list(command)
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output = (proc.stdout + proc.stderr)[-MAX_OUTPUT_CHARS:]
        return TestRun(
            command=command,
            exit_code=proc.returncode,
            output=output,
            failing_tests=_parse_failures(output, command),
        )
    except subprocess.TimeoutExpired as exc:
        output = ""
        if exc.output:
            output = exc.output.decode("utf-8", "replace") if isinstance(
                exc.output, bytes) else str(exc.output)
        if exc.stderr:
            err = exc.stderr.decode("utf-8", "replace") if isinstance(
                exc.stderr, bytes) else str(exc.stderr)
            output += err
        return TestRun(
            command=command,
            exit_code=None,
            output=output[-MAX_OUTPUT_CHARS:],
            failing_tests=_parse_failures(output, command),
            timed_out=True,
        )


def _git(repo: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=60
    )


@dataclass
class _Worktree:
    """A temporary git worktree, removed on exit whatever happens."""

    repo: str
    root: str = field(default="")
    checkout: str = field(default="")

    def __enter__(self) -> str:
        self.root = tempfile.mkdtemp(prefix="sentinel-ci-verify-")
        self.checkout = os.path.join(self.root, "checkout")
        return self.checkout

    def __exit__(self, *exc) -> None:
        if self.checkout:
            _git(self.repo, "worktree", "remove", "--force", self.checkout)
        if self.root:
            shutil.rmtree(self.root, ignore_errors=True)


def run_sandbox(
    repo_path: str,
    head_sha: str,
    patch: str,
    failing_tests: list[str],
    test_command: str = "",
    timeout: int = 600,
    image: str = "python:3.12-slim",
) -> SandboxResult:
    """Apply ``patch`` at ``head_sha`` in isolation and run the tests.

    Never mutates ``repo_path``'s working tree. The result records
    which sandbox mode ran and both test runs (targeted, then full);
    interpreting them as gate conditions is the caller's job.
    """
    mode = "docker" if docker_available() else "worktree"
    result = SandboxResult(mode=mode)

    if not repo_path or not os.path.isdir(repo_path):
        result.error = f"no local checkout at {repo_path!r} to verify against"
        return result
    if _git(repo_path, "rev-parse", "--git-dir").returncode != 0:
        result.error = f"{repo_path!r} is not a git repository"
        return result
    sha = head_sha or "HEAD"
    if _git(repo_path, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        result.error = f"commit {sha!r} not found in {repo_path!r}"
        return result

    base_command = shlex.split(test_command) if test_command else shlex.split(
        detect_test_command(repo_path)
    )
    targeted_cmd, full_cmd = _test_commands(base_command, failing_tests)

    with _Worktree(repo_path) as checkout:
        add = _git(repo_path, "worktree", "add", "--detach", checkout, sha)
        if add.returncode != 0:
            result.error = f"could not check out {sha}: {add.stderr.strip()[:300]}"
            return result

        patch_file = os.path.join(os.path.dirname(checkout), "repair.patch")
        with open(patch_file, "w", encoding="utf-8") as fh:
            fh.write(patch)
        applied = subprocess.run(
            ["git", "-C", checkout, "apply", "--whitespace=nowarn", patch_file],
            capture_output=True, text=True, timeout=60,
        )
        if applied.returncode != 0:
            result.patch_error = (applied.stdout + applied.stderr).strip()[:500]
            return result
        result.patch_applied = True

        result.targeted = _run(targeted_cmd, checkout, timeout, mode, image)
        result.full = _run(full_cmd, checkout, timeout, mode, image)
    return result
