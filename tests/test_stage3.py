"""Stage 3 tests: the verification gate, fully offline.

The sandbox runs for real: each test builds a throwaway git repo
from ``tests/fixtures/project`` (a small shop project whose checkout
calls a renamed pricing function), and the gate applies recorded
Repair patches to a clean worktree of the failing commit and runs
the project's actual test suite there. The Gemini transport seam is
stubbed with recorded adversary responses, exactly as in Stage 2, so
the only model in the loop is the recorded one. Docker is not
available in this environment, so the worktree fallback is the mode
exercised; the mode assertions accept whichever mode the sandbox
reports for the host, and one test pins the mode to the host's
Docker availability.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from sentinel_ci.agents.adversarial_verification import adversarial_verification
from sentinel_ci.config import Config
from sentinel_ci.sandbox import docker_available, normalize_test_id, run_sandbox
from sentinel_ci.state import new_state

try:
    from tests.test_stage2 import recorded_text, stubbed
except ImportError:  # discovery without the tests package prefix
    from test_stage2 import recorded_text, stubbed

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
PROJECT = os.path.join(FIXTURES, "project")

CONFIG = Config(
    gemini_api_key="test-key",
    github_token="test-token",
    repo="owner/shop",
    verify_timeout_seconds=120,
)

FAILING_TEST = "tests/test_cart.py::test_checkout_total"
EXCERPT = (
    "tests/test_cart.py:21: in test_checkout_total\n"
    "    assert cart.checkout() == 100\n"
    "cart.py:18: in checkout\n"
    "    return pricing.total_price(self.items)\n"
    "E       AttributeError: module 'pricing' has no attribute "
    "'total_price'. Did you mean: 'compute_total'?"
)
ROOT_CAUSE = (
    "pricing.total_price was renamed to compute_total; "
    "cart.py still calls the old name"
)


def git(repo: str, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def make_repo(tmp: str) -> tuple[str, str]:
    """The fixture project as a git repo; returns (path, head sha)."""
    repo = os.path.join(tmp, "shop")
    shutil.copytree(PROJECT, repo)
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "fixture@example.com")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fixture project at the failing commit")
    return repo, git(repo, "rev-parse", "HEAD")


def fixture_text(name: str) -> str:
    with open(os.path.join(PROJECT, name), encoding="utf-8") as fh:
        return fh.read()


def incident_state(repo: str, sha: str, **overrides):
    base = dict(
        repo="owner/shop",
        run_id=100,
        head_branch="main",
        head_sha=sha,
        base_sha="green123",
        repo_path=repo,
        failing_tests=[FAILING_TEST],
        error_excerpts=[EXCERPT],
        failure_summary=f"1 failing test(s): {FAILING_TEST}; exit code 1",
        exit_code=1,
        changed_files=["pricing.py"],
        diff=(
            "diff --git a/pricing.py b/pricing.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def total_price(items):\n"
            "+def compute_total(items):\n"
            "     return sum(item.price for item in items)"
        ),
        code_context=[
            {"path": "tests/test_cart.py", "content": fixture_text("tests/test_cart.py"),
             "note": "whole file"},
            {"path": "cart.py", "content": fixture_text("cart.py"),
             "note": "whole file"},
            {"path": "pricing.py", "content": fixture_text("pricing.py"),
             "note": "whole file"},
        ],
        root_cause=ROOT_CAUSE,
        root_cause_confidence=0.93,
        status="diagnosing",
    )
    base.update(overrides)
    return new_state(**base)


def gate_state(repo: str, sha: str, patch_fixture: str, **overrides):
    """An incident as Repair leaves it: patch proposed, gate not run."""
    base = dict(
        repair_patch=recorded_text(patch_fixture),
        repair_files=["cart.py"],
        repair_attempts=1,
        status="awaiting_verification",
    )
    base.update(overrides)
    return incident_state(repo, sha, **base)


def expected_mode() -> str:
    return "docker" if docker_available() else "worktree"


class SandboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sentinel-ci-test-")
        self.repo, self.sha = make_repo(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_good_patch_runs_green_in_isolation(self) -> None:
        result = run_sandbox(
            self.repo, self.sha, recorded_text("repair_stage3_good.json"),
            [FAILING_TEST], timeout=120,
        )
        self.assertEqual(result.mode, expected_mode())
        self.assertTrue(result.patch_applied, result.output_tail)
        self.assertIsNotNone(result.targeted)
        self.assertIsNotNone(result.full)
        self.assertEqual(result.targeted.failing_tests, [])
        self.assertEqual(result.full.failing_tests, [])
        self.assertEqual(result.full.exit_code, 0)

    def test_original_checkout_is_never_mutated(self) -> None:
        run_sandbox(
            self.repo, self.sha, recorded_text("repair_stage3_good.json"),
            [FAILING_TEST], timeout=120,
        )
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertIn("pricing.total_price", fixture_text("cart.py"))
        with open(os.path.join(self.repo, "cart.py"), encoding="utf-8") as fh:
            self.assertIn("pricing.total_price", fh.read())
        worktrees = git(self.repo, "worktree", "list").splitlines()
        self.assertEqual(len(worktrees), 1)  # only the main checkout remains

    def test_unappliable_patch_is_reported_not_raised(self) -> None:
        bogus = (
            "diff --git a/cart.py b/cart.py\n--- a/cart.py\n+++ b/cart.py\n"
            "@@ -1,3 +1,3 @@\n-this context does not exist\n"
            "-nor does this\n+replacement\n"
        )
        result = run_sandbox(self.repo, self.sha, bogus, [FAILING_TEST], timeout=120)
        self.assertFalse(result.patch_applied)
        self.assertTrue(result.patch_error)
        self.assertIsNone(result.targeted)

    def test_unknown_commit_is_an_error_result(self) -> None:
        result = run_sandbox(
            self.repo, "deadbeef" * 5, recorded_text("repair_stage3_good.json"),
            [FAILING_TEST], timeout=120,
        )
        self.assertTrue(result.error)
        self.assertFalse(result.patch_applied)


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sentinel-ci-test-")
        self.repo, self.sha = make_repo(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_correct_fix_passes_all_three_conditions(self) -> None:
        state = gate_state(self.repo, self.sha, "repair_stage3_good.json")
        with stubbed({"Adversarial Verification agent": ["adversary_survives.json"]}):
            updates = adversarial_verification(state, config=CONFIG)
        self.assertEqual(updates["verification_verdict"], "passed")
        self.assertEqual(updates["status"], "verified")
        self.assertEqual(updates["sandbox_mode"], expected_mode())
        self.assertEqual(updates["targeted_failures"], [])
        self.assertEqual(updates["new_failures"], [])
        self.assertEqual(updates["adversary"]["verdict"], "survives")
        self.assertTrue(updates["adversary"]["attack"])
        actions = [e["action"] for e in updates["audit"]]
        self.assertIn("tests_green", actions)
        self.assertIn("gate_passed", actions)

    def test_regression_fix_is_sent_back_by_the_suite(self) -> None:
        state = gate_state(self.repo, self.sha, "repair_stage3_regression.json")
        # No adversary route is stubbed: if the gate paid for an
        # adversary call on a patch the suite already killed, the
        # stub raises and this test fails.
        with stubbed({}):
            updates = adversarial_verification(state, config=CONFIG)
        self.assertEqual(updates["verification_verdict"], "failed")
        self.assertNotEqual(updates.get("status"), "verified")
        self.assertEqual(updates["targeted_failures"], [])
        self.assertEqual(
            {normalize_test_id(t) for t in updates["new_failures"]},
            {("tests/test_pricing.py", "test_tax_for")},
        )
        self.assertTrue(
            any("broke previously passing" in n for n in updates["verification_notes"])
        )
        actions = [e["action"] for e in updates["audit"]]
        self.assertIn("gate_failed", actions)

    def test_adversary_breaks_a_green_symptom_hiding_fix(self) -> None:
        state = gate_state(self.repo, self.sha, "repair_stage3_hardcode.json")
        with stubbed({"Adversarial Verification agent": ["adversary_broken.json"]}):
            updates = adversarial_verification(state, config=CONFIG)
        # The suite was green; the gate still failed, on condition 3.
        self.assertEqual(updates["targeted_failures"], [])
        self.assertEqual(updates["new_failures"], [])
        self.assertEqual(updates["verification_verdict"], "failed")
        self.assertNotEqual(updates.get("status"), "verified")
        self.assertEqual(updates["adversary"]["verdict"], "broken")
        self.assertIn("literal 100", updates["adversary"]["attack"])
        actions = [e["action"] for e in updates["audit"]]
        self.assertIn("tests_green", actions)
        self.assertIn("adversary_broke_fix", actions)

    def test_unavailable_adversary_is_inconclusive_never_verified(self) -> None:
        state = gate_state(self.repo, self.sha, "repair_stage3_good.json")
        updates = adversarial_verification(state, config=Config())
        self.assertEqual(updates["verification_verdict"], "inconclusive")
        self.assertNotEqual(updates.get("status"), "verified")

    def test_gate_without_a_checkout_stays_pending(self) -> None:
        state = gate_state(self.repo, self.sha, "repair_stage3_good.json")
        del state["repo_path"]
        updates = adversarial_verification(state, config=CONFIG)
        self.assertNotIn("verification_verdict", updates)
        self.assertEqual(updates.get("status", state["status"]), "awaiting_verification")
        actions = [e["action"] for e in updates["audit"]]
        self.assertIn("gate_pending", actions)


class Stage3GraphTest(unittest.TestCase):
    """The full Stage 2 + 3 flow through the fallback runner."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp(prefix="sentinel-ci-test-")
        self.repo, self.sha = make_repo(self._tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run_graph(self, routes):
        from sentinel_ci.graph import build_graph
        from sentinel_ci.github_client import GitHubError

        class OfflineGitHub:
            def last_green_sha(self, repo, branch, before_sha):
                return "green123"

            def compare_diff(self, repo, base, head):
                raise GitHubError("offline in tests")

            def get_file_text(self, repo, path, ref):
                raise GitHubError("offline in tests")

        state = incident_state(self.repo, self.sha)
        with mock.patch(
            "sentinel_ci.agents.codebase_analyst.GitHubClient",
            lambda token: OfflineGitHub(),
        ), stubbed(routes):
            return build_graph(CONFIG).invoke(state)

    def test_incident_flows_to_a_verified_fix(self) -> None:
        result = self._run_graph(
            {
                "Failure Hypothesis agent": ["hypothesis_renamed.json"],
                "Root Cause agent": ["root_cause_confirmed.json"],
                "Repair agent": ["repair_stage3_good.json"],
                "Adversarial Verification agent": ["adversary_survives.json"],
            }
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["verification_verdict"], "passed")
        self.assertEqual(result["repair_attempts"], 1)
        self.assertEqual(result["sandbox_mode"], expected_mode())
        actions = [e["action"] for e in result["audit"]]
        self.assertIn("gate_passed", actions)

    def test_adversary_break_loops_back_and_second_attempt_verifies(self) -> None:
        result = self._run_graph(
            {
                "Failure Hypothesis agent": ["hypothesis_renamed.json"],
                "Root Cause agent": ["root_cause_confirmed.json"],
                "Repair agent": [
                    "repair_stage3_hardcode.json",
                    "repair_stage3_good.json",
                ],
                "Adversarial Verification agent": [
                    "adversary_broken.json",
                    "adversary_survives.json",
                ],
            }
        )
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["repair_attempts"], 2)
        actions = [e["action"] for e in result["audit"]]
        self.assertIn("adversary_broke_fix", actions)
        self.assertIn("gate_passed", actions)
        self.assertTrue(
            any("adversary broke the fix" in n for n in result["verification_notes"])
        )

    def test_attempts_exhausted_escalates_with_the_evidence(self) -> None:
        result = self._run_graph(
            {
                "Failure Hypothesis agent": ["hypothesis_renamed.json"],
                "Root Cause agent": ["root_cause_confirmed.json"],
                "Repair agent": [
                    "repair_stage3_regression.json",
                    "repair_stage3_regression.json",
                ],
            }
        )
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(result["repair_attempts"], 2)
        self.assertIn("Verification gate did not pass", result["escalation_reason"])
        self.assertEqual(
            {normalize_test_id(t) for t in result["new_failures"]},
            {("tests/test_pricing.py", "test_tax_for")},
        )
        actions = [e["action"] for e in result["audit"]]
        self.assertEqual(actions.count("gate_failed"), 2)
        self.assertIn("escalated", actions)


if __name__ == "__main__":
    unittest.main()
