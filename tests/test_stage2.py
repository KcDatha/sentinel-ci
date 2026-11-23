"""Stage 2 tests: diagnosis and repair agents, fully offline.

No live GEMINI_API_KEY exists in the test environment, so the Gemini
transport seam (``sentinel_ci.llm._generate``) is stubbed with
recorded API responses from ``tests/fixtures/gemini/``. The stub
routes on the agent's system prompt, exactly like the real calls, so
prompt building, JSON parsing, schema validation, the patch guard,
and graph routing all run for real. The GitHub client is faked with
the same compare and contents data the API would return.
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import sentinel_ci.llm as llm_module
from sentinel_ci.agents.codebase_analyst import codebase_analyst
from sentinel_ci.agents.failure_hypothesis import failure_hypothesis
from sentinel_ci.agents.repair import repair
from sentinel_ci.agents.root_cause import root_cause
from sentinel_ci.config import Config
from sentinel_ci.github_client import GitHubError
from sentinel_ci.patch_guard import validate_patch
from sentinel_ci.state import new_state

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def recorded(name: str) -> dict:
    with open(fixture(os.path.join("gemini", name)), encoding="utf-8") as fh:
        return json.load(fh)


def recorded_text(name: str) -> str:
    return recorded(name)["candidates"][0]["content"]["parts"][0]["text"]


class StubTransport:
    """Replaces llm._generate. Routes recorded responses by agent.

    ``routes`` maps a marker from the agent's system prompt to a
    queue of fixture filenames, popped in call order.
    """

    def __init__(self, routes: dict[str, list[str]]):
        self._routes = {k: list(v) for k, v in routes.items()}
        self.calls: list[str] = []

    def __call__(self, config: Config, payload: dict) -> dict:
        system = payload["systemInstruction"]["parts"][0]["text"]
        for marker, queue in self._routes.items():
            if marker in system:
                self.calls.append(marker)
                if not queue:
                    raise AssertionError(f"stub queue exhausted for {marker}")
                return recorded(queue.pop(0))
        raise AssertionError(f"no stubbed response for system prompt: {system[:80]}")


def stubbed(routes: dict[str, list[str]]):
    return mock.patch.object(llm_module, "_generate", StubTransport(routes))


CONFIG = Config(
    gemini_api_key="test-key", github_token="test-token", repo="owner/shop"
)

CART_PY = "\n".join(
    [
        "import pricing",  # 1
        "",  # 2
        "",  # 3
        "class Item:",  # 4
        "    def __init__(self, price):",  # 5
        "        self.price = price",  # 6
        "",  # 7
        "",  # 8
        "class Cart:",  # 9
        "    def __init__(self, items):",  # 10
        "        self.items = items",  # 11
        "",  # 12
        "    def add(self, item):",  # 13
        "        self.items.append(item)",  # 14
        "",  # 15
        "    def checkout(self):",  # 16
        "        # total via the pricing module",  # 17
        "        return pricing.total_price(self.items)",  # 18
    ]
)

PRICING_PY = "\n".join(
    [
        "def compute_total(items):",
        "    return sum(item.price for item in items)",
        "",
        "def tax_for(amount):",
        "    return amount * 0.1",
    ]
)

TEST_CART_PY = "\n".join(
    [
        "from cart import Cart, Item",
        "",
        "",
        "def test_checkout_total():",
        "    cart = Cart([Item(50), Item(50)])",
        "    assert cart.checkout() == 100",
    ]
)

COMPARE = {
    "files": [
        {
            "filename": "pricing.py",
            "status": "modified",
            "patch": "@@ -1,3 +1,3 @@\n"
            "-def total_price(items):\n"
            "+def compute_total(items):\n"
            "     return sum(item.price for item in items)",
        }
    ]
}

EXCERPT = (
    "tests/test_cart.py:21: in test_checkout_total\n"
    "    assert cart.checkout() == 100\n"
    "cart.py:18: in checkout\n"
    "    return pricing.total_price(self.items)\n"
    "E       AttributeError: module 'pricing' has no attribute "
    "'total_price'. Did you mean: 'compute_total'?"
)


class FakeGitHubClient:
    """The slice of the GitHub client the analyst depends on."""

    def __init__(self, compare=None, files=None, green="green123"):
        self._compare = COMPARE if compare is None else compare
        self._files = (
            {"cart.py": CART_PY, "pricing.py": PRICING_PY, "tests/test_cart.py": TEST_CART_PY}
            if files is None
            else files
        )
        self._green = green

    def last_green_sha(self, repo: str, branch: str, before_sha: str):
        return self._green

    def compare_diff(self, repo: str, base: str, head: str) -> dict:
        return self._compare

    def get_file_text(self, repo: str, path: str, ref: str) -> str:
        if path not in self._files:
            raise GitHubError(f"404 for {path}")
        return self._files[path]


def incident_state(**overrides):
    base = dict(
        repo="owner/shop",
        run_id=99,
        head_branch="main",
        head_sha="head999aaa",
        failing_tests=["tests/test_cart.py::test_checkout_total"],
        error_excerpts=[EXCERPT],
        failure_summary="1 failing test(s): tests/test_cart.py::test_checkout_total; exit code 1",
        exit_code=1,
        status="diagnosing",
    )
    base.update(overrides)
    return new_state(**base)


def diagnosed_state(**overrides):
    """An incident state as the analyst leaves it for the shop repo."""
    state = incident_state()
    state.update(codebase_analyst(state, config=CONFIG, client=FakeGitHubClient()))
    state.update(overrides)
    return state


class CodebaseAnalystTest(unittest.TestCase):
    def test_diff_and_context_collected(self) -> None:
        state = incident_state()
        updates = codebase_analyst(state, config=CONFIG, client=FakeGitHubClient())
        self.assertEqual(updates["base_sha"], "green123")
        self.assertEqual(updates["changed_files"], ["pricing.py"])
        self.assertIn("compute_total", updates["diff"])
        self.assertFalse(updates["diff_truncated"])
        paths = [r["path"] for r in updates["code_context"]]
        self.assertEqual(paths[0], "tests/test_cart.py")
        self.assertIn("cart.py", paths)
        cart_region = next(r for r in updates["code_context"] if r["path"] == "cart.py")
        self.assertIn("pricing.total_price(self.items)", cart_region["content"])
        self.assertIn("around line 18", cart_region["note"])

    def test_no_coordinates_degrades_honestly(self) -> None:
        state = new_state(repo="local/log", log_text="boom")
        updates = codebase_analyst(state, config=CONFIG, client=FakeGitHubClient())
        self.assertNotIn("diff", updates)
        actions = [e["action"] for e in updates["audit"]]
        self.assertIn("diff_unavailable", actions)

    def test_oversized_diff_is_truncated_and_noted(self) -> None:
        big = {"files": [{"filename": "big.py", "status": "modified",
                          "patch": "+" + "x" * 40_000}]}
        state = incident_state()
        updates = codebase_analyst(
            state, config=CONFIG, client=FakeGitHubClient(compare=big)
        )
        self.assertTrue(updates["diff_truncated"])
        self.assertLessEqual(len(updates["diff"]), 24_000)


class FailureHypothesisTest(unittest.TestCase):
    def test_structured_hypotheses_parsed(self) -> None:
        state = diagnosed_state()
        with stubbed({"Failure Hypothesis agent": ["hypothesis_renamed.json"]}):
            updates = failure_hypothesis(state, config=CONFIG)
        self.assertEqual(updates["hypothesis_attempts"], 1)
        top = updates["hypotheses"][0]
        self.assertIn("renamed", top["cause"])
        self.assertEqual(top["confidence"], 0.88)
        self.assertTrue(top["supporting_evidence"])
        self.assertTrue(top["falsifier"])

    def test_malformed_output_produces_no_hypotheses(self) -> None:
        state = diagnosed_state()
        with stubbed({"Failure Hypothesis agent": ["malformed.json"]}):
            updates = failure_hypothesis(state, config=CONFIG)
        self.assertEqual(updates["hypotheses"], [])
        actions = [e["action"] for e in updates["audit"]]
        self.assertIn("diagnosis_unavailable", actions)

    def test_missing_key_produces_no_hypotheses(self) -> None:
        state = diagnosed_state()
        updates = failure_hypothesis(state, config=Config())
        self.assertEqual(updates["hypotheses"], [])


class RootCauseTest(unittest.TestCase):
    def _state_with_hypothesis(self, fixture_name: str, **overrides):
        state = diagnosed_state()
        with stubbed({"Failure Hypothesis agent": [fixture_name]}):
            state.update(failure_hypothesis(state, config=CONFIG))
        state.update(overrides)
        return state

    def test_confirmed_root_cause(self) -> None:
        state = self._state_with_hypothesis("hypothesis_renamed.json")
        with stubbed({"Root Cause agent": ["root_cause_confirmed.json"]}):
            updates = root_cause(state, config=CONFIG)
        self.assertEqual(updates["root_cause_verdict"], "confirmed")
        self.assertIn("compute_total", updates["root_cause"])
        self.assertEqual(updates["root_cause_confidence"], 0.93)
        self.assertNotEqual(updates.get("status"), "escalated")

    def test_rejected_hypothesis_routes_back(self) -> None:
        state = self._state_with_hypothesis("hypothesis_wrong.json")
        with stubbed({"Root Cause agent": ["root_cause_rejected.json"]}):
            updates = root_cause(state, config=CONFIG)
        self.assertEqual(updates["root_cause_verdict"], "rejected")
        self.assertEqual(len(updates["rejected_causes"]), 1)
        self.assertIn("discount", updates["rejected_causes"][0])
        self.assertNotEqual(updates.get("status"), "escalated")

    def test_rejected_with_attempts_exhausted_escalates(self) -> None:
        state = self._state_with_hypothesis(
            "hypothesis_wrong.json", hypothesis_attempts=2
        )
        with stubbed({"Root Cause agent": ["root_cause_rejected.json"]}):
            updates = root_cause(state, config=CONFIG)
        self.assertEqual(updates["status"], "escalated")
        self.assertIn("exhausted", updates["escalation_reason"])

    def test_confirmed_below_threshold_escalates(self) -> None:
        state = self._state_with_hypothesis("hypothesis_renamed.json")
        with stubbed({"Root Cause agent": ["root_cause_low_confidence.json"]}):
            updates = root_cause(state, config=CONFIG)
        self.assertEqual(updates["status"], "escalated")
        self.assertIn("below the", updates["escalation_reason"])

    def test_no_hypotheses_escalates(self) -> None:
        updates = root_cause(diagnosed_state(), config=CONFIG)
        self.assertEqual(updates["status"], "escalated")


class PatchGuardTest(unittest.TestCase):
    def test_minimal_caller_fix_passes(self) -> None:
        verdict = validate_patch(
            recorded_text("repair_renamed.json"),
            allowed_files={"cart.py", "pricing.py", "tests/test_cart.py"},
        )
        self.assertTrue(verdict.ok, verdict.problems)
        self.assertEqual(verdict.files, ["cart.py"])

    def test_symptom_patch_with_skip_is_rejected(self) -> None:
        verdict = validate_patch(
            recorded_text("repair_symptom.json"),
            allowed_files={"tests/test_cart.py"},
        )
        self.assertFalse(verdict.ok)
        self.assertTrue(any("skip" in p for p in verdict.problems))

    def test_unrelated_file_is_rejected(self) -> None:
        patch = (
            "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        verdict = validate_patch(patch, allowed_files={"cart.py"})
        self.assertFalse(verdict.ok)
        self.assertTrue(any("unrelated" in p for p in verdict.problems))

    def test_deleted_test_file_is_rejected(self) -> None:
        patch = (
            "diff --git a/tests/test_cart.py b/tests/test_cart.py\n"
            "--- a/tests/test_cart.py\n+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n-def test_checkout_total():\n-    assert True\n"
        )
        verdict = validate_patch(patch, allowed_files={"tests/test_cart.py"})
        self.assertFalse(verdict.ok)
        self.assertTrue(any("deletes test file" in p for p in verdict.problems))

    def test_removed_test_function_is_rejected(self) -> None:
        patch = (
            "diff --git a/tests/test_cart.py b/tests/test_cart.py\n"
            "--- a/tests/test_cart.py\n+++ b/tests/test_cart.py\n"
            "@@ -4,3 +4,2 @@\n-def test_checkout_total():\n-    assert cart.checkout() == 100\n"
            " def test_add_item():\n"
        )
        verdict = validate_patch(patch, allowed_files={"tests/test_cart.py"})
        self.assertFalse(verdict.ok)
        self.assertTrue(any("test function" in p for p in verdict.problems))

    def test_oversized_patch_is_rejected(self) -> None:
        body = "\n".join(f"+line {i}" for i in range(300))
        patch = f"diff --git a/cart.py b/cart.py\n--- a/cart.py\n+++ b/cart.py\n@@ -0,0 +1,300 @@\n{body}\n"
        verdict = validate_patch(patch, allowed_files={"cart.py"})
        self.assertFalse(verdict.ok)
        self.assertTrue(any("cap" in p for p in verdict.problems))

    def test_empty_patch_is_rejected(self) -> None:
        self.assertFalse(validate_patch("").ok)
        self.assertFalse(validate_patch("no diff here").ok)


class RepairAgentTest(unittest.TestCase):
    def _root_caused_state(self, **overrides):
        state = diagnosed_state()
        state.update(
            root_cause="pricing.total_price was renamed to compute_total; "
            "cart.py still calls the old name",
            root_cause_confidence=0.93,
        )
        state.update(overrides)
        return state

    def test_minimal_patch_proposed_and_held_at_gate(self) -> None:
        state = self._root_caused_state()
        with stubbed({"Repair agent": ["repair_renamed.json"]}):
            updates = repair(state, config=CONFIG)
        self.assertEqual(updates["status"], "awaiting_verification")
        self.assertIn("pricing.compute_total", updates["repair_patch"])
        self.assertEqual(updates["repair_files"], ["cart.py"])
        self.assertEqual(updates["repair_attempts"], 1)

    def test_symptom_patch_rejected_by_guard_before_verification(self) -> None:
        state = self._root_caused_state()
        with stubbed({"Repair agent": ["repair_symptom.json"]}):
            updates = repair(state, config=CONFIG)
        self.assertEqual(updates["status"], "escalated")
        self.assertNotIn("repair_patch", updates)
        self.assertIn("guard rejected", updates["escalation_reason"])
        actions = [e["action"] for e in updates["audit"]]
        self.assertIn("patch_rejected_by_guard", actions)


class Stage2GraphTest(unittest.TestCase):
    """The whole Stage 2 flow through the fallback runner, offline."""

    def _run_graph(self, routes):
        from sentinel_ci.graph import build_graph

        with open(fixture("renamed_function.log"), encoding="utf-8") as fh:
            log_text = fh.read()
        state = new_state(
            repo="owner/shop",
            head_branch="main",
            head_sha="head999aaa",
            log_text=log_text,
            status="diagnosing",
        )
        fake_client = FakeGitHubClient()
        with mock.patch(
            "sentinel_ci.agents.codebase_analyst.GitHubClient",
            lambda token: fake_client,
        ), stubbed(routes):
            return build_graph(CONFIG).invoke(state)

    def test_incident_flows_to_a_proposed_patch(self) -> None:
        result = self._run_graph(
            {
                "Failure Hypothesis agent": ["hypothesis_renamed.json"],
                "Root Cause agent": ["root_cause_confirmed.json"],
                "Repair agent": ["repair_renamed.json"],
            }
        )
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertIn("compute_total", result["root_cause"])
        self.assertIn("pricing.compute_total", result["repair_patch"])
        self.assertEqual(result["changed_files"], ["pricing.py"])
        self.assertEqual(result["hypothesis_attempts"], 1)
        actions = [e["action"] for e in result["audit"]]
        self.assertIn("gate_pending", actions)

    def test_rejected_hypothesis_loops_and_recovers(self) -> None:
        result = self._run_graph(
            {
                "Failure Hypothesis agent": [
                    "hypothesis_wrong.json",
                    "hypothesis_renamed.json",
                ],
                "Root Cause agent": [
                    "root_cause_rejected.json",
                    "root_cause_confirmed.json",
                ],
                "Repair agent": ["repair_renamed.json"],
            }
        )
        self.assertEqual(result["status"], "awaiting_verification")
        self.assertEqual(result["hypothesis_attempts"], 2)
        self.assertEqual(len(result["rejected_causes"]), 1)
        self.assertIn("pricing.compute_total", result["repair_patch"])

    def test_symptom_patch_escalates_through_the_graph(self) -> None:
        result = self._run_graph(
            {
                "Failure Hypothesis agent": ["hypothesis_renamed.json"],
                "Root Cause agent": ["root_cause_confirmed.json"],
                "Repair agent": ["repair_symptom.json"],
            }
        )
        self.assertEqual(result["status"], "escalated")
        self.assertFalse(result.get("repair_patch"))
        self.assertIn("guard rejected", result["escalation_reason"])


if __name__ == "__main__":
    unittest.main()
