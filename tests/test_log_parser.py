"""Stage 1 tests: the log parser against realistic Actions logs, and
the watcher against a fake GitHub client. Stdlib unittest only, so
the suite runs anywhere:  python3 -m unittest discover -s tests -t .
"""

from __future__ import annotations

import os
import unittest

from sentinel_ci.log_parser import parse_log_file, parse_log_text, strip_timestamps
from sentinel_ci.watcher import Watcher

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


class PytestFailureLogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.summary = parse_log_file(fixture("pytest_failure.log"))

    def test_failing_tests_extracted_in_order(self) -> None:
        self.assertEqual(
            self.summary.failing_tests,
            [
                "tests/test_cart.py::test_total_with_discount",
                "tests/test_pricing.py::test_tax_rounding",
            ],
        )

    def test_exit_code_extracted(self) -> None:
        self.assertEqual(self.summary.exit_code, 1)

    def test_excerpts_carry_the_assertions(self) -> None:
        joined = "\n".join(self.summary.error_excerpts)
        self.assertIn("AssertionError: assert 90.0 == 100.0", joined)
        self.assertIn("Decimal('10.005')", joined)

    def test_summary_line(self) -> None:
        self.assertIn("2 failing test(s)", self.summary.summary_line)
        self.assertIn("exit code 1", self.summary.summary_line)


class GenericFailureLogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.summary = parse_log_file(fixture("generic_failure.log"))

    def test_no_failing_tests(self) -> None:
        self.assertEqual(self.summary.failing_tests, [])

    def test_exit_code_extracted(self) -> None:
        self.assertEqual(self.summary.exit_code, 2)

    def test_error_lines_captured(self) -> None:
        joined = "\n".join(self.summary.error_excerpts)
        self.assertIn("error:", joined)
        self.assertIn("tax_rate", joined)

    def test_summary_line(self) -> None:
        self.assertIn("exit code 2", self.summary.summary_line)
        self.assertIn("no test failures detected", self.summary.summary_line)


class ParserEdgeCasesTest(unittest.TestCase):
    def test_empty_log(self) -> None:
        summary = parse_log_text("")
        self.assertEqual(summary.failing_tests, [])
        self.assertIsNone(summary.exit_code)
        self.assertIn("no structured failure markers", summary.summary_line)

    def test_timestamps_stripped(self) -> None:
        lines = strip_timestamps("2026-11-22T04:12:03.1234567Z hello world")
        self.assertEqual(lines, ["hello world"])

    def test_plain_text_without_timestamps(self) -> None:
        summary = parse_log_text("FAILED tests/test_a.py::test_b - boom\n")
        self.assertEqual(summary.failing_tests, ["tests/test_a.py::test_b"])


class FakeClient:
    """The watcher's client protocol, backed by a fixture log."""

    def __init__(self, log_text: str):
        self._log_text = log_text
        self._runs = [
            {
                "id": 4242,
                "name": "CI",
                "head_branch": "main",
                "head_sha": "abc123",
                "conclusion": "failure",
            }
        ]

    def list_failed_runs(self, repo: str, per_page: int = 20) -> list[dict]:
        return list(self._runs)

    def download_logs_text(self, repo: str, run_id: int) -> str:
        return self._log_text


class WatcherTest(unittest.TestCase):
    def test_poll_once_reports_new_failure_once(self) -> None:
        with open(fixture("pytest_failure.log"), encoding="utf-8") as fh:
            log_text = fh.read()
        reports_seen = []
        watcher = Watcher(FakeClient(log_text), "owner/repo", reports_seen.append)

        first = watcher.poll_once()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].state["run_id"], 4242)
        self.assertEqual(len(first[0].summary.failing_tests), 2)
        self.assertEqual(first[0].state["status"], "diagnosing")

        second = watcher.poll_once()
        self.assertEqual(second, [])
        self.assertEqual(len(reports_seen), 1)


class GraphSkeletonTest(unittest.TestCase):
    def test_graph_runs_and_escalates_honestly(self) -> None:
        from sentinel_ci.config import Config
        from sentinel_ci.graph import build_graph
        from sentinel_ci.state import new_state

        with open(fixture("pytest_failure.log"), encoding="utf-8") as fh:
            log_text = fh.read()
        graph = build_graph(Config())
        result = graph.invoke(new_state(repo="owner/repo", log_text=log_text))
        # Diagnosis is Stage 2, so the skeleton must escalate, and the
        # Stage 1 evidence must be in the state when it does.
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(len(result["failing_tests"]), 2)
        self.assertTrue(result["audit"])


if __name__ == "__main__":
    unittest.main()
