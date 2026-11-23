"""Stage 4 pipeline: one failed run in, one healed incident out.

This is the path the GitHub Action runs. It takes an incident state
built from a failed workflow run (Stage 1 evidence), gives it a local
checkout of the target repository so the Stage 3 gate can run, walks
the agent graph (diagnosis, repair, verification), and, only when the
incident comes back ``verified``, has the Mission Controller publish
the fix as a pull request (:mod:`sentinel_ci.pull_request`).

A publishing failure never unmakes a verified fix: it is recorded in
the audit trail and the incident stays ``verified`` with no PR, so a
human can open one by hand from the same evidence.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

from .config import Config
from .github_client import GitHubClient
from .graph import build_graph
from .pull_request import PullRequestError, open_pull_request
from .state import SentinelCIState, audit_event
from .watcher import Watcher


def heal_incident(
    state: SentinelCIState, config: Config, client: GitHubClient
) -> SentinelCIState:
    """Run the graph over one incident, then publish a verified fix."""
    result = dict(build_graph(config).invoke(state))
    if result.get("status") == "verified":
        try:
            result.update(open_pull_request(result, client))  # type: ignore[arg-type]
        except PullRequestError as exc:
            audit = list(result.get("audit", []))
            audit.append(
                audit_event(
                    "Mission Controller",
                    "pr_failed",
                    f"fix is verified but publishing failed: {exc}",
                )
            )
            result["audit"] = audit
    return result  # type: ignore[return-value]


def incident_for_run(
    client: GitHubClient, repo: str, run_id: int | None = None
) -> SentinelCIState | None:
    """Incident state for one failed run (the newest, or ``run_id``).

    Reuses the Stage 1 watcher so the evidence pack is built exactly
    the way the polling path builds it.
    """
    captured: list = []
    watcher = Watcher(client, repo, captured.append)
    reports = watcher.poll_once()
    for report in reports:
        if run_id is None or report.state.get("run_id") == run_id:
            return report.state
    return None


def ensure_checkout(config: Config, state: SentinelCIState) -> str:
    """A local git checkout of the incident repo at the failing commit.

    The Stage 3 gate needs one. ``SENTINEL_CI_REPO_PATH`` wins when
    set (the Action checks the consumer repo out itself); otherwise
    the repo is cloned into a temporary directory with the configured
    token. The token bearing clone URL is never printed or stored in
    the state. Returns the checkout path ("" when none could be made,
    in which case the gate holds the incident untrusted, as designed).
    """
    if config.repo_path:
        return config.repo_path
    repo = state.get("repo", "")
    if not repo or not config.github_token:
        return ""
    root = tempfile.mkdtemp(prefix="sentinel-ci-heal-")
    dest = os.path.join(root, "repo")
    url = f"https://x-access-token:{config.github_token}@github.com/{repo}.git"
    proc = subprocess.run(
        ["git", "clone", "--quiet", url, dest],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        shutil.rmtree(root, ignore_errors=True)
        return ""
    sha = state.get("head_sha", "")
    if sha:
        subprocess.run(
            ["git", "-C", dest, "checkout", "--quiet", sha],
            capture_output=True, text=True, timeout=120,
        )
    return dest
