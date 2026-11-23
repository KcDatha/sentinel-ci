"""Command line entry point: ``python -m sentinel_ci ...``

Commands available today (Stages 1 to 4):

    parse-log FILE     parse a saved Actions log and print the summary
    watch              poll the configured repo for failed runs
                       (--once for a single pass)
    run-graph          run the agent graph over a saved log file and
                       print the final incident state (with
                       GEMINI_API_KEY set, Stage 2 diagnosis and
                       repair run; the Stage 3 verification gate runs
                       when the incident carries a local checkout of
                       the repository, and otherwise the proposed
                       patch stays held at the gate, untrusted;
                       without a key, the incident escalates honestly
                       with the evidence attached)
    heal               the full Stage 4 pipeline over a failed run of
                       the configured repo: evidence, diagnosis,
                       repair, verification, and a pull request for a
                       verified fix. This is what the GitHub Action
                       runs. The run is the one named by the
                       workflow_run event payload when present
                       (GITHUB_EVENT_PATH), else the newest failed
                       run; --run-id pins one explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .config import Config
from .github_client import GitHubClient
from .graph import build_graph
from .log_parser import parse_log_file
from .pipeline import ensure_checkout, heal_incident, incident_for_run
from .state import new_state
from .watcher import FailureReport, Watcher


def _print_report(report: FailureReport) -> None:
    s = report.summary
    print(f"[sentinel-ci] run {report.state.get('run_id')} failed: {s.summary_line}")
    for test in s.failing_tests:
        print(f"  failing: {test}")


def cmd_parse_log(args: argparse.Namespace) -> int:
    summary = parse_log_file(args.file)
    print(json.dumps(summary.to_dict(), indent=2))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if args.repo:
        config.repo = args.repo
    problems = config.problems()
    if problems:
        print("cannot watch: " + "; ".join(problems), file=sys.stderr)
        return 2
    client = GitHubClient(config.github_token)
    watcher = Watcher(client, config.repo, _print_report)
    if args.once:
        watcher.poll_once()
    else:  # pragma: no cover - long running by definition
        watcher.watch(config.poll_interval_seconds)
    return 0


def cmd_run_graph(args: argparse.Namespace) -> int:
    config = Config.from_env()
    with open(args.file, encoding="utf-8", errors="replace") as fh:
        log_text = fh.read()
    state = new_state(repo=config.repo or "local/log", log_text=log_text, status="diagnosing")
    result = build_graph(config).invoke(state)
    printable = {k: v for k, v in result.items() if k != "log_text"}
    print(json.dumps(printable, indent=2, default=str))
    return 0


def _event_run_id() -> int | None:
    """The failed run id from a workflow_run event payload, if any."""
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        run = payload.get("workflow_run") or {}
        return int(run["id"]) if run.get("id") else None
    except (OSError, ValueError, TypeError):
        return None


def cmd_heal(args: argparse.Namespace) -> int:
    config = Config.from_env()
    if args.repo:
        config.repo = args.repo
    if not config.gemini_api_key:
        # Free tier key missing is a configuration state, not a crash:
        # say exactly what to do and change nothing.
        print(
            "SENTINEL CI: GEMINI_API_KEY is not set, so the diagnosis "
            "agents cannot run. Add a free Google AI Studio key as the "
            "GEMINI_API_KEY secret in this repository (Settings > "
            "Secrets and variables > Actions > New repository secret), "
            "then re-run the failed workflow. No changes were made."
        )
        return 0
    problems = config.problems()
    if problems:
        print("cannot heal: " + "; ".join(problems), file=sys.stderr)
        return 2
    client = GitHubClient(config.github_token)
    run_id = args.run_id or _event_run_id()
    state = incident_for_run(client, config.repo, run_id)
    if state is None:
        print("[sentinel-ci] no failed run found to heal")
        return 0
    checkout = ensure_checkout(config, state)
    if checkout:
        state["repo_path"] = checkout
    result = heal_incident(state, config, client)
    status = result.get("status", "")
    print(
        f"[sentinel-ci] run {result.get('run_id')}: "
        f"{result.get('failure_summary', '')} -> {status}"
    )
    if result.get("pr_url"):
        print(f"[sentinel-ci] pull request opened: {result['pr_url']}")
    if status == "escalated":
        print(f"[sentinel-ci] escalated: {result.get('escalation_reason', '')}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel_ci", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse-log", help="parse a saved Actions log file")
    p.add_argument("file")
    p.set_defaults(func=cmd_parse_log)

    p = sub.add_parser("watch", help="watch a repo for failed runs")
    p.add_argument("--repo", help="owner/name (default: SENTINEL_CI_REPO)")
    p.add_argument("--once", action="store_true", help="single polling pass")
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("run-graph", help="run the agent graph over a log file")
    p.add_argument("file")
    p.set_defaults(func=cmd_run_graph)

    p = sub.add_parser("heal", help="heal one failed run end to end (Stage 4)")
    p.add_argument("--repo", help="owner/name (default: SENTINEL_CI_REPO)")
    p.add_argument("--run-id", type=int, help="failed run id (default: event payload or newest)")
    p.set_defaults(func=cmd_heal)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
