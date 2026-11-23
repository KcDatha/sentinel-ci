# SENTINEL CI: Build Plan

**Concept.** When a GitHub Actions run fails, SENTINEL CI reads the failure
logs and the diff since the last green build, diagnoses the root cause,
writes a minimal repair, and an Adversarial Verification agent tries to
break the fix (rerunning the failing tests plus the regression suite in a
clean environment) before a pull request opens. The fix ships only if the
challenge fails to break it. If nothing verifies, the incident escalates
to a human with the full evidence trail attached.

**Lineage.** SENTINEL CI reuses the six agent architecture from SENTINEL
(Guru's autonomous software reliability project) and points it at CI:

| # | Agent | Job in SENTINEL CI |
|---|-------|--------------------|
| 01 | Mission Controller | Opens the incident from a failed run, owns routing, opens the PR or escalates |
| 02 | Codebase Analyst | Fetches the diff since the last green build, maps blast radius, seals the evidence pack |
| 03 | Failure Hypothesis | Ranked candidate causes, each with the observation that would confirm or kill it |
| 04 | Root Cause | Tests hypotheses against logs, excerpts, and the diff until one cause explains every symptom |
| 05 | Repair | Drafts the smallest safe fix: patch, blast radius, rollback path, regression coverage |
| 06 | Adversarial Verification | Reruns failing tests plus the regression suite in Docker and argues against the fix; gates the PR |

**Stack.**

- Python 3.10+ (developed on 3.12)
- LangGraph state graph: one typed state object every agent reads and writes
- Gemini API (Google AI Studio, free tier) for the diagnosis and repair
  agents; key read from `GEMINI_API_KEY`, default model `gemini-2.5-flash`
- GitHub REST API for runs, logs, compare, and pull requests
- Docker for clean verification runs (Stage 3)
- Packaged as a GitHub Action: consumers add one workflow file (see
  `examples/sentinel-ci.yml`)

**Free tiers only (hard constraint).** Every part of this project runs on
free tiers, in development and for consumers: the Gemini free tier for
model calls, GitHub free for hosting, GitHub Actions free minutes for
runs, and Vercel free for the demo page. Design decisions that would
require a paid tier (a bigger model by default, long running hosted
workers, paid CI minutes) are out of scope by rule, not by accident.
Free tier quotas are a design input: the watcher polls politely, model
calls are budgeted per incident, and the demo page is a static deploy.

**Environment note (honest).** In the current development sandbox,
`langgraph` and `pytest` are not installed; `requests` and `pyyaml` are.
The code is built to degrade gracefully rather than pretend: the graph
falls back to a built in runner that walks the identical topology
(`sentinel_ci/graph.py`), the Gemini client speaks plain HTTPS with no
vendor SDK (`sentinel_ci/llm.py`), the GitHub client falls back to stdlib
`urllib`, and the test suite uses stdlib `unittest`. `requirements.txt`
and `pyproject.toml` declare the real dependencies for consumers and CI.

---

## Stage 1: Watcher and log parser — DONE in this pass

- [x] `sentinel_ci/watcher.py`: polls the GitHub API for completed runs
      with conclusion `failure`, deduplicates by run id (optional JSON
      state file), downloads logs, and emits a populated incident state.
- [x] `sentinel_ci/log_parser.py`: pure stdlib parser. Strips runner
      timestamps; extracts failing pytest node ids (short summary and
      inline reports), traceback tail excerpts from the FAILURES/ERRORS
      sections, `##[error]` annotations, the process exit code, and a one
      line summary. Non test failures fall back to generic error lines.
- [x] `sentinel_ci/github_client.py`: thin REST client (runs, logs zip,
      last green sha, compare) over `requests` or `urllib`.
- [x] Tests with two realistic fixtures (pytest failure, generic nonzero
      exit), plus watcher and graph skeleton tests. All passing.
- [x] CLI: `python -m sentinel_ci parse-log FILE`, `watch [--once]`,
      `run-graph FILE`.

## Stage 2: Diagnosis and repair agents

- [ ] Codebase Analyst: compare API diff (last green sha to failed head),
      changed files, and relevant code context into the evidence pack.
- [ ] Failure Hypothesis: structured LLM output; every hypothesis carries
      supporting evidence and a falsifier. Reject hypotheses the evidence
      cannot support.
- [ ] Root Cause: score hypotheses against failing tests, excerpts, and
      the diff; keep only a cause that explains every symptom; calibrated
      confidence. Below the threshold (default 0.7) the incident escalates.
- [ ] Repair: minimal patch as a unified diff, plus blast radius, rollback
      path, and the regression tests that should guard it.
- [ ] Prompt and output contracts under `sentinel_ci/prompts/` with
      schema validation on every model response; malformed output routes
      back, never forward.
- [ ] Unit tests with recorded model responses (no live API in CI).

## Stage 3: Verification loop

- [ ] Docker runner: checkout at the failed head sha, apply the patch,
      run the originally failing tests, then the repo's regression suite.
- [ ] Adversarial Verification agent: verdict plus notes arguing against
      the root cause and the patch (wrong layer, masked symptom, new
      breakage). Verdicts: passed, failed, inconclusive. Inconclusive
      never ships.
- [ ] Retry loop: failed gate returns to Repair with the verifier's
      notes; attempts capped (default 2), then escalate.
- [ ] Cost and time budgets per incident; every model call and test run
      logged against the incident state (auditability by construction).

## Stage 4: Pull requests, demo, launch

- [ ] Controller opens a PR from the verified patch only: title, root
      cause, evidence, verification transcript, rollback path. Never
      auto merge. Draft PR when confidence is mid range.
- [ ] `action.yml` published flow: tag v1, Marketplace listing draft.
- [ ] Demo repository with intentionally failing tests (a discount bug
      and a rounding bug, matching the parser fixtures' world), plus a
      recorded end to end run: red build, diagnosis, repair, verifier
      rejecting the first attempt, verified fix, PR.
- [ ] Live demo page (see UI design direction below): replays an
      incident end to end with the agent pipeline visible. Static site
      in `demo/`, deploy ready for Vercel free tier (`vercel.json`
      points at it); a first version ships with this plan.
- [ ] Launch package: README final pass, demo GIF or short screen
      recording, LinkedIn post draft for Guru's approval before posting.

## Guardrails (non negotiable, from the SENTINEL pattern)

- The engineering is in the brakes: no PR without a passed verification
  gate, no auto merge ever, attempt caps, confidence thresholds.
- A confident wrong answer is worse than an escalation. When in doubt,
  the system hands a human the evidence trail and stops.
- Secrets come from the environment only; logs are treated as untrusted
  input (prompt injection via log text is a named threat; log content is
  data for the agents, never instructions).
- Every agent action is written to the incident audit trail.

## UI design direction

Direction from Guru, applying to the Stage 4 demo page and every UI
surface in this project:

- **Subtle, muted colors only.** No bright or saturated colors anywhere.
  No gradients, no glow effects, no glassmorphism, nothing from the
  generic AI app aesthetic.
- **Quiet neutrals as the base.** Near white and soft gray surfaces,
  near black text, hairline borders. Color is used rarely and on purpose.
- **One restrained accent.** A desaturated steel blue, used for links,
  the active agent, and the verified state, and almost nothing else.
- **Real typography.** A proper sans for prose (system stack or Inter),
  a proper monospace for logs, code, and data. Hierarchy comes from
  weight, size, and spacing, not from color.
- **Generous whitespace.** Calm, uncluttered layouts; density only where
  a developer tool earns it (log and diff views).
- **Feel.** Human made, like a carefully crafted developer tool.
  Reference points are GitHub and Linear restraint. If a screen could be
  mistaken for a generated AI landing page, it is wrong.

Working tokens for the demo page (starting point, all desaturated):

| Token | Value | Use |
|-------|-------|-----|
| background | #fbfbfc | page |
| surface | #ffffff | panels |
| surface muted | #f4f5f6 | code and log blocks |
| border | #e2e5e8 | hairlines, panel edges |
| text | #24282d | primary text |
| text muted | #68707a | secondary text, timestamps |
| accent | #4a6b8a | links, active agent, verified |
| failure | #9d5a52 | failed state only, muted brick |
| success | #5d7d61 | verified state only, muted green |

The README follows the same restraint in presentation: plain headings,
no badge walls, no decorative imagery.

## Risks and open questions

- Log formats vary by ecosystem; the parser starts with pytest and
  generic exit codes, and grows per ecosystem (jest, go test, cargo)
  only with real fixtures in hand.
- Verification cost: a full regression rerun per attempt is the honest
  gate and the main cost driver; budgets in Stage 3 keep it bounded.
- GitHub App vs Action token permissions: the Action route ships first;
  a GitHub App is the follow up if polling and permissions chafe.
