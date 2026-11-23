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

Under active development. Stage 1 (watcher, log parser) and Stage 2
(diagnosis and repair) are implemented: the Codebase Analyst seals an
evidence pack from the diff since the last green build, Failure
Hypothesis and Root Cause argue a cause to a calibrated confidence
(with a bounded retry loop when a hypothesis is rejected), and Repair
drafts a minimal patch that a programmatic guard checks before it
exists (evidence implicated files only, size caps, and no weakened,
skipped, or deleted tests). A proposed patch stops at
`awaiting_verification`: the Docker verification loop (Stage 3) and
pull request opening (Stage 4) are not built yet, so no fix is
trusted or shipped. When diagnosis cannot run or cannot reach
confidence, the pipeline does the honest thing: it escalates to a
human with the evidence attached. The full plan, including
guardrails and the UI design direction for the demo page, is in
[BUILD_PLAN.md](BUILD_PLAN.md).

## Author

**Guru Datha Kandanathi Chittem** — AI Engineer | Multi-Agent Systems, RAG & LLM Evals
[Portfolio](https://gurudatha-portfolio.vercel.app/) · [LinkedIn](https://www.linkedin.com/in/gurudatha-kandanathi-chittem/)
