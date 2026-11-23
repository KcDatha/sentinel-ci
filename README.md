# SENTINEL CI

A self healing CI agent. When a GitHub Actions run fails, SENTINEL CI
reads the failure logs and the diff since the last green build,
diagnoses the root cause, writes a minimal repair, and then tries to
break its own fix before a pull request opens.

SENTINEL CI is the [SENTINEL](https://github.com/KcDatha/sentinel) six
agent architecture pointed at CI pipelines: a Mission Controller
dispatches specialist agents, and an Adversarial Verification agent
has to fail to break the repair before anyone trusts it. The
engineering is in the brakes.

## How it works

| # | Agent | Job |
|---|-------|-----|
| 01 | Mission Controller | Opens the incident from the failed run, owns routing, opens the PR or escalates |
| 02 | Codebase Analyst | Fetches the diff since the last green build and maps the blast radius |
| 03 | Failure Hypothesis | Proposes ranked candidate causes, each with the observation that would kill it |
| 04 | Root Cause | Tests hypotheses against the evidence until one cause explains every symptom |
| 05 | Repair | Drafts the smallest safe fix, with blast radius and a rollback path |
| 06 | Adversarial Verification | Reruns the failing tests plus the regression suite in a clean environment and argues against the fix |

All six agents read and write one typed state object (LangGraph), so
every conclusion traces back to its evidence in an audit trail.

## The verification gate

A repair becomes a pull request only if all of this holds:

- the originally failing tests pass with the patch applied,
- the regression suite passes in a clean Docker environment,
- the verification agent's challenge (wrong layer, masked symptom, new
  breakage) fails to break the fix.

Anything else loops back to Repair with the verifier's notes, a capped
number of times, and then escalates to a human with the full evidence
trail. SENTINEL CI never auto merges.

## Install the Action

Add `.github/workflows/sentinel-ci.yml` to your repository (full file
in [`examples/sentinel-ci.yml`](examples/sentinel-ci.yml)):

```yaml
name: SENTINEL CI
on:
  workflow_run:
    workflows: ["CI"]
    types: [completed]
permissions:
  actions: read
  contents: write
  pull-requests: write
jobs:
  heal:
    if: ${{ github.event.workflow_run.conclusion == 'failure' }}
    runs-on: ubuntu-latest
    steps:
      - uses: KcDatha/sentinel-ci@v1
        with:
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
```

`GEMINI_API_KEY` is a free Google AI Studio key. SENTINEL CI runs on
free tiers only: Gemini free tier for model calls, GitHub free and
Actions free minutes for the pipeline, Vercel free for the demo page.

## Run it locally

Stage 1 (watcher and log parser) and Stage 2 (diagnosis and repair)
work today, on the standard library alone apart from the Gemini call:

```bash
# Parse a saved Actions log into a compact failure summary
python -m sentinel_ci parse-log path/to/run.log

# Watch a repository for failed runs (one pass)
GITHUB_TOKEN=... SENTINEL_CI_REPO=owner/name python -m sentinel_ci watch --once

# Run the agent graph over a log and print the incident state
# (with GEMINI_API_KEY set, diagnosis and repair run end to end)
GEMINI_API_KEY=... python -m sentinel_ci run-graph path/to/run.log

# Tests
python -m unittest discover -s tests -t .
```

## Status

Under active development. Stages 1 to 3 are implemented: the watcher
and log parser (Stage 1); diagnosis and repair (Stage 2: the Codebase
Analyst seals an evidence pack from the diff since the last green
build, Failure Hypothesis and Root Cause argue a cause to a calibrated
confidence with a bounded retry loop, and Repair drafts a minimal
patch that a programmatic guard checks before it exists); and the
verification gate (Stage 3). A proposed patch is applied to a clean
checkout of the failing commit in an isolated sandbox (Docker when
available, otherwise a temporary git worktree; the original checkout
is never mutated) and earns trust on three conditions, all required:
the originally failing tests pass, the full suite shows no new
failures, and the Adversarial Verification agent, briefed to make the
strongest case that the fix is wrong or only hiding the symptom,
fails to break it. A failed gate feeds its evidence back into the
next repair attempt within a capped budget, then escalates to a human
with the audit trail. Only a fix that survives all three conditions
is marked `verified`; pull request opening (Stage 4) is not built
yet, so nothing ships from here. When diagnosis cannot run or cannot
reach confidence, the pipeline does the honest thing: it escalates to
a human with the evidence attached. The full plan, including
guardrails and the UI design direction for the demo page, is in
[BUILD_PLAN.md](BUILD_PLAN.md).

## Author

**Guru Datha Kandanathi Chittem** — AI Engineer | Multi-Agent Systems, RAG & LLM Evals
[Portfolio](https://gurudatha-portfolio.vercel.app/) · [LinkedIn](https://www.linkedin.com/in/gurudatha-kandanathi-chittem/)
