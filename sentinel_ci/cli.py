"""Command line entry point: ``python -m sentinel_ci ...``

Commands available today (Stages 1 and 2):

    parse-log FILE     parse a saved Actions log and print the summary
    watch              poll the configured repo for failed runs
                       (--once for a single pass)
    run-graph          run the agent graph over a saved log file and
                       print the final incident state (with
                       GEMINI_API_KEY set, Stage 2 diagnosis and
                       repair run and a proposed patch stops at the
                       verification gate; without it, the incident
                       escalates honestly with the evidence attached)
"""

from __future__ import annotations

import argparse
import json
import sys

from .config import Config
from .github_client import GitHubClient
from .graph import build_graph
from .log_parser import parse_log_file
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
