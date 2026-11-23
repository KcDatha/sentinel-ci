"""Stage 1: watch a repository for failed GitHub Actions runs.

The watcher polls the GitHub API, and for every newly failed run it
downloads the logs, runs them through the Stage 1 log parser, and hands
a populated incident state to a callback (the Mission Controller in the
full pipeline; a printer in the CLI today).

Seen run ids are kept in memory and optionally in a small JSON file, so
a restart does not reprocess old failures.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .log_parser import FailureSummary, parse_log_text
from .state import SentinelCIState, audit_event, new_state


class RunsClient(Protocol):
    """The slice of the GitHub client the watcher depends on."""

    def list_failed_runs(self, repo: str, per_page: int = 20) -> list[dict]: ...

    def download_logs_text(self, repo: str, run_id: int) -> str: ...


@dataclass
class FailureReport:
    """One newly detected failed run, with its parsed failure summary."""

    state: SentinelCIState
    summary: FailureSummary


class Watcher:
    def __init__(
        self,
        client: RunsClient,
        repo: str,
        on_failure: Callable[[FailureReport], None],
        state_file: str | None = None,
    ):
        self._client = client
        self._repo = repo
        self._on_failure = on_failure
        self._state_file = state_file
        self._seen: set[int] = self._load_seen()

    # -- persistence ---------------------------------------------------
    def _load_seen(self) -> set[int]:
        if self._state_file and os.path.exists(self._state_file):
            try:
                with open(self._state_file, encoding="utf-8") as fh:
                    return {int(x) for x in json.load(fh)}
            except (ValueError, TypeError):
                return set()
        return set()

    def _save_seen(self) -> None:
        if self._state_file:
            with open(self._state_file, "w", encoding="utf-8") as fh:
                json.dump(sorted(self._seen), fh)

    # -- core ------------------------------------------------------------
    def poll_once(self) -> list[FailureReport]:
        """One polling pass. Returns reports for runs not seen before."""
        reports: list[FailureReport] = []
        for run in self._client.list_failed_runs(self._repo):
            run_id = int(run["id"])
            if run_id in self._seen:
                continue
            self._seen.add(run_id)
            log_text = self._client.download_logs_text(self._repo, run_id)
            summary = parse_log_text(log_text)
            state = new_state(
                repo=self._repo,
                run_id=run_id,
                workflow_name=run.get("name", ""),
                head_branch=run.get("head_branch", ""),
                head_sha=run.get("head_sha", ""),
                log_text=log_text,
                failing_tests=summary.failing_tests,
                error_excerpts=summary.error_excerpts,
                failure_summary=summary.summary_line,
                exit_code=summary.exit_code,
                status="diagnosing",
            )
            state["audit"] = [
                audit_event(
                    "Mission Controller",
                    "incident_opened",
                    f"run {run_id} failed: {summary.summary_line}",
                )
            ]
            report = FailureReport(state=state, summary=summary)
            reports.append(report)
            self._on_failure(report)
        self._save_seen()
        return reports

    def watch(self, poll_interval_seconds: int = 60) -> None:  # pragma: no cover
        """Poll forever. The CLI wraps this; tests use poll_once."""
        while True:
            self.poll_once()
            time.sleep(poll_interval_seconds)
