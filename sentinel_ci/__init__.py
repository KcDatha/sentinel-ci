"""SENTINEL CI: a self healing CI agent.

When a GitHub Actions run fails, SENTINEL CI reads the failure logs and
the diff since the last green build, diagnoses the root cause, drafts a
minimal repair, and lets an Adversarial Verification agent try to break
the fix before a pull request opens. It is the SENTINEL six agent
architecture (Mission Controller, Codebase Analyst, Failure Hypothesis,
Root Cause, Repair, Adversarial Verification) pointed at CI pipelines.

Status: under active development. Stage 1 (watcher and log parser) is
implemented; the diagnosis, repair, verification, and pull request
stages are scaffolded and land stage by stage per BUILD_PLAN.md.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
