"""Stage 4 tests: pull request opening and the heal pipeline, offline.

The GitHub client is stubbed with an in memory fake that records
every call the PR stage makes, so the assertions cover exactly what
would reach the API: the branch name, the tree built from the
verified patch, the commit, and the pull request title and body. The
graph is stubbed at the pipeline seam for the wiring tests, so no
model and no network are involved anywhere in this file.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from sentinel_ci import cli
from sentinel_ci.config import Config
from sentinel_ci.pipeline import heal_incident
from sentinel_ci.pull_request import (
    PatchApplyError,
    PullRequestError,
    apply_file_diff,
    build_pr_body,
    build_pr_title,
    open_pull_request,
    parse_diff_hunks,
)
from sentinel_ci.state import audit_event, new_state

HEAD_SHA = "f" * 40
TREE_SHA = "t" * 40

CART_BEFORE = (
    "import pricing\n"
    "\n"
    "\n"
    "class Cart:\n"
    "    def checkout(self):\n"
    "        return pricing.total_price(self.items)\n"
)
PATCH = (
    "diff --git a/cart.py b/cart.py\n"
    "--- a/cart.py\n"
    "+++ b/cart.py\n"
    "@@ -4,3 +4,3 @@\n"
    " class Cart:\n"
    "     def checkout(self):\n"
    "-        return pricing.total_price(self.items)\n"
    "+        return pricing.compute_total(self.items)\n"
)
ROOT_CAUSE = (
    "pricing.total_price was renamed to compute_total; "
    "cart.py still calls the old name"
)
ATTACK = (
    "the fix updates the one call site in cart.py, but a second "
    "caller could still use the old name"
)


def verified_state(**overrides):
    state = new_state(
        repo="owner/shop",
        run_id=4242,
        workflow_name="CI",
        head_branch="main",
        head_sha=HEAD_SHA,
        failure_summary="1 failing test(s): tests/test_cart.py::test_checkout_total",
        failing_tests=["tests/test_cart.py::test_checkout_total"],
        error_excerpts=["E       AttributeError: module 'pricing' has no attribute 'total_price'"],
        hypotheses=[{
            "cause": "a rename left a stale caller",
            "supporting_evidence": ["the traceback names total_price"],
            "falsifier": "another module defining total_price",
            "confidence": 0.9,
            "confidence_note": "the diff shows the rename directly",
        }],
        root_cause=ROOT_CAUSE,
        root_cause_confidence=0.9,
        repair_patch=PATCH,
        repair_files=["cart.py"],
        repair_attempts=1,
        hypothesis_attempts=1,
        verification_verdict="passed",
        sandbox_mode="worktree",
        targeted_failures=[],
        new_failures=[],
        adversary={"verdict": "survives", "attack": ATTACK,
                   "reasoning": "no second caller exists in the diff"},
        verification_notes=["attempt 1: fix survived the adversary"],
        status="verified",
    )
    state["audit"] = [
        audit_event("Mission Controller", "incident_opened", "run 4242 failed"),
        audit_event("Adversarial Verification", "gate_passed", "all three conditions held"),
    ]
    state.update(overrides)
    return state


class StubClient:
    """The slice of GitHubClient the PR stage uses, recording calls."""

    def __init__(self, files=None):
        self.files = files if files is not None else {"cart.py": CART_BEFORE}
        self.calls: list[tuple] = []
        self.blobs: dict[str, str] = {}
        self.trees: list[dict] = []
        self.commits: list[dict] = []
        self.refs: list[tuple] = []
        self.pulls: list[dict] = []

    def get_commit(self, repo, sha):
        self.calls.append(("get_commit", repo, sha))
        return {"tree": {"sha": TREE_SHA}}

    def get_file_text(self, repo, path, ref):
        self.calls.append(("get_file_text", repo, path, ref))
        return self.files[path]

    def create_blob(self, repo, text):
        sha = f"blob-{len(self.blobs)}"
        self.blobs[sha] = text
        self.calls.append(("create_blob", repo))
        return sha

    def create_tree(self, repo, base_tree, entries):
        self.trees.append({"base_tree": base_tree, "entries": entries})
        self.calls.append(("create_tree", repo))
        return "new-tree-sha"

    def create_commit(self, repo, message, tree, parents):
        self.commits.append({"message": message, "tree": tree, "parents": parents})
        self.calls.append(("create_commit", repo))
        return "new-commit-sha"

    def create_ref(self, repo, ref, sha):
        self.refs.append((ref, sha))
        self.calls.append(("create_ref", repo))
        return {"ref": f"refs/{ref}"}

    def get_repo(self, repo):
        return {"default_branch": "main"}

    def create_pull_request(self, repo, title, body, head, base, draft=False):
        self.pulls.append(
            {"title": title, "body": body, "head": head, "base": base,
             "draft": draft}
        )
        self.calls.append(("create_pull_request", repo))
        return {"html_url": "https://github.com/owner/shop/pull/7"}


class TestOpenPullRequest(unittest.TestCase):
    def test_branch_title_and_url(self):
        client = StubClient()
        updates = open_pull_request(verified_state(), client)
        self.assertEqual(client.refs, [("heads/sentinel-ci/run-4242", "new-commit-sha")])
        self.assertEqual(len(client.pulls), 1)
        pr = client.pulls[0]
        self.assertEqual(pr["head"], "sentinel-ci/run-4242")
        self.assertEqual(pr["base"], "main")
        self.assertTrue(pr["title"].startswith("fix:"))
        self.assertIn("test_checkout_total", pr["title"])
        self.assertLessEqual(len(pr["title"]), 72)
        self.assertEqual(updates["pr_url"], "https://github.com/owner/shop/pull/7")
        self.assertEqual(updates["status"], "ready_for_pr")

    def test_body_is_the_audit_trail(self):
        body = build_pr_body(verified_state())
        self.assertIn(ROOT_CAUSE, body)                      # confirmed root cause
        self.assertIn("Hypothesis:", body)
        self.assertIn("Originally failing tests pass", body)  # gate condition 1
        self.assertIn("No new failures", body)                # gate condition 2
        self.assertIn("adversary failed to break the fix", body.lower())  # condition 3
        self.assertIn(ATTACK, body)                           # the attack that failed
        self.assertIn("pricing.compute_total", body)          # the diff itself
        self.assertIn("Repair attempts: 1", body)             # attempt history
        self.assertIn("never auto merges", body)

    def test_patch_is_applied_to_the_fetched_contents(self):
        client = StubClient()
        open_pull_request(verified_state(), client)
        (blob_text,) = client.blobs.values()
        self.assertIn("pricing.compute_total(self.items)", blob_text)
        self.assertNotIn("total_price", blob_text)
        tree = client.trees[0]
        self.assertEqual(tree["base_tree"], TREE_SHA)
        self.assertEqual(tree["entries"][0]["path"], "cart.py")
        self.assertEqual(client.commits[0]["parents"], [HEAD_SHA])

    def test_mid_range_confidence_opens_a_draft(self):
        client = StubClient()
        open_pull_request(verified_state(root_cause_confidence=0.75), client)
        self.assertTrue(client.pulls[0]["draft"])

    def test_high_confidence_opens_a_ready_pr(self):
        client = StubClient()
        open_pull_request(verified_state(root_cause_confidence=0.9), client)
        self.assertFalse(client.pulls[0]["draft"])

    def test_unverified_incident_is_refused(self):
        client = StubClient()
        with self.assertRaises(PullRequestError):
            open_pull_request(verified_state(status="escalated",
                                             verification_verdict="failed"), client)
        self.assertEqual(client.pulls, [])
        self.assertEqual(client.refs, [])

    def test_awaiting_verification_is_refused(self):
        client = StubClient()
        with self.assertRaises(PullRequestError):
            open_pull_request(verified_state(status="awaiting_verification",
                                             verification_verdict=""), client)
        self.assertEqual(client.pulls, [])


class TestDiffApplication(unittest.TestCase):
    def test_modify(self):
        (fd,) = parse_diff_hunks(PATCH)
        out = apply_file_diff(CART_BEFORE, fd)
        self.assertIn("compute_total", out)
        self.assertTrue(out.endswith("\n"))

    def test_create_file(self):
        patch = (
            "diff --git a/newmod.py b/newmod.py\n"
            "--- /dev/null\n"
            "+++ b/newmod.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def answer():\n"
            "+    return 42\n"
        )
        (fd,) = parse_diff_hunks(patch)
        self.assertEqual(fd.old_path, "")
        self.assertEqual(apply_file_diff("", fd), "def answer():\n    return 42\n")

    def test_delete_file(self):
        patch = (
            "diff --git a/old.py b/old.py\n"
            "--- a/old.py\n"
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n"
            "-legacy = True\n"
        )
        (fd,) = parse_diff_hunks(patch)
        self.assertEqual(fd.new_path, "")
        self.assertEqual(apply_file_diff("legacy = True\n", fd), "")

    def test_context_mismatch_refuses(self):
        (fd,) = parse_diff_hunks(PATCH)
        with self.assertRaises(PatchApplyError):
            apply_file_diff("completely\ndifferent\ncontents\n", fd)

    def test_title_caps_and_counts(self):
        state = verified_state(failing_tests=[
            "tests/a.py::test_one", "tests/b.py::test_two",
            "tests/c.py::test_three", "tests/d.py::test_four",
        ])
        title = build_pr_title(state)
        self.assertIn("(+1 more)", title)
        self.assertLessEqual(len(title), 72)


class TestHealPipeline(unittest.TestCase):
    def _config(self):
        return Config(gemini_api_key="test-key", github_token="gh-test",
                      repo="owner/shop")

    def test_verified_incident_opens_pr(self):
        final = verified_state()
        fake_graph = mock.Mock()
        fake_graph.invoke.return_value = dict(final)
        client = StubClient()
        with mock.patch("sentinel_ci.pipeline.build_graph", return_value=fake_graph):
            result = heal_incident(new_state(repo="owner/shop"), self._config(), client)
        self.assertEqual(result["status"], "ready_for_pr")
        self.assertEqual(result["pr_url"], "https://github.com/owner/shop/pull/7")
        self.assertEqual(len(client.pulls), 1)

    def test_escalated_incident_opens_nothing(self):
        fake_graph = mock.Mock()
        fake_graph.invoke.return_value = dict(
            verified_state(status="escalated", verification_verdict="failed")
        )
        client = StubClient()
        with mock.patch("sentinel_ci.pipeline.build_graph", return_value=fake_graph):
            result = heal_incident(new_state(repo="owner/shop"), self._config(), client)
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(client.pulls, [])

    def test_publishing_failure_keeps_the_fix_verified(self):
        fake_graph = mock.Mock()
        fake_graph.invoke.return_value = dict(verified_state())
        client = StubClient()
        with mock.patch("sentinel_ci.pipeline.build_graph", return_value=fake_graph), \
             mock.patch("sentinel_ci.pipeline.open_pull_request",
                        side_effect=PullRequestError("api down")):
            result = heal_incident(new_state(repo="owner/shop"), self._config(), client)
        self.assertEqual(result["status"], "verified")
        self.assertFalse(result.get("pr_url"))
        self.assertTrue(
            any(e["action"] == "pr_failed" for e in result["audit"])
        )


class TestHealCliGraceful(unittest.TestCase):
    def test_missing_gemini_key_is_a_clear_line_not_a_crash(self):
        env = {"GITHUB_TOKEN": "gh-test", "SENTINEL_CI_REPO": "owner/shop",
               "PATH": os.environ.get("PATH", "")}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            code = cli.main(["heal"])
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("GEMINI_API_KEY is not set", text)
        self.assertIn("No changes were made", text)


if __name__ == "__main__":
    unittest.main()
