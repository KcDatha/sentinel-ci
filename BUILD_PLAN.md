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

## Stage 2: Diagnosis and repair agents — DONE

- [x] Codebase Analyst: compare API diff (last green sha to failed
      head; the base is resolved from the most recent successful run
      when the watcher did not record it), changed files, and code
      regions around the failure (failing test files plus the source
      files traceback frames point into, read at the failing commit).
      Bounded: diff capped at 24k chars, at most 6 context files,
      slices capped at 4k chars; every truncation is recorded in the
      state (`diff_truncated`, `context_truncated`). Missing
      coordinates or an unreachable API degrade to log evidence only,
      recorded in the audit trail.
- [x] Failure Hypothesis: structured Gemini output (JSON mode);
      every hypothesis carries supporting evidence, a falsifier, a
      calibrated confidence, and a confidence note. Schema validated
      in code; malformed output produces no hypotheses and the
      incident escalates rather than guessing forward.
- [x] Root Cause: a second Gemini call challenges the top hypothesis
      against every symptom. Confirmed at or above the threshold
      (default 0.7): on to Repair. Confirmed below it: escalate.
      Rejected: the dead cause is recorded in `rejected_causes` and
      the graph routes back to Failure Hypothesis for another
      attempt (default cap 2, `SENTINEL_CI_MAX_HYPOTHESIS_ATTEMPTS`);
      exhausted attempts escalate.
- [x] Repair: minimal patch as a unified diff, produced only for a
      confirmed root cause. Before the patch enters the state, the
      programmatic guard (`sentinel_ci/patch_guard.py`) enforces the
      load bearing rules in code: parseable diff, only files the
      evidence implicates, size caps (5 files, 250 changed lines),
      and no weakening, skipping, or deleting tests (no deleted test
      files, no removed test functions, no net assertion loss, no
      skip or xfail markers anywhere). A rejected patch is discarded
      and the incident escalates with the guard's reasons; it never
      reaches verification.
- [x] Prompt contracts in `sentinel_ci/prompts.py` (system prompts
      and builders for all three agents), schema validation on every
      model response; malformed output routes back or escalates,
      never forward. Log and diff content is fenced as untrusted
      data in every prompt.
- [x] Unit tests with recorded Gemini responses stubbed at the
      transport seam (`sentinel_ci.llm._generate`): a renamed
      function failure healed by updating the caller, a rejected
      first hypothesis that loops and recovers, a symptom only
      patch (skip marker plus weakened assertion) rejected by the
      guard, and the full flow through the fallback graph. No live
      API in tests.
- [x] An incident flows from Stage 1 evidence to a proposed patch
      and stops at `awaiting_verification` until the Stage 3 gate
      judges it (Stage 3 below): no verdict is recorded by Stage 2,
      and nothing is trusted yet.

## Stage 3: Verification loop — DONE

- [x] Sandbox runner (`sentinel_ci/sandbox.py`): the incident repo is
      checked out at the failing commit into a temporary `git
      worktree`, the proposed patch is applied there, and the project
      tests run in that tree; the original checkout is never mutated
      and the worktree is removed afterwards. When a `docker` binary
      is available the test commands execute inside a container with
      the patched tree mounted (default image `python:3.12-slim`,
      `SENTINEL_CI_SANDBOX_IMAGE`); otherwise the same worktree runs
      them with the host interpreter. The mode that ran is recorded
      in the state (`sandbox_mode`), never assumed. Every run is
      timeout bounded (`SENTINEL_CI_VERIFY_TIMEOUT`, default 600s);
      a timeout is a failed condition, never a pass. The test command
      is configured (`SENTINEL_CI_TEST_COMMAND`) or autodetected
      (pytest when the project declares it or it is installed, else
      unittest discovery). Honest environment note: this development
      sandbox has no Docker, so the test suite exercises the worktree
      fallback end to end; the Docker path shares all of the code
      except the command wrapper.
- [x] The three gate conditions, all required, in
      `sentinel_ci/agents/adversarial_verification.py`: (1) the
      originally failing tests pass under the patch (targeted run
      over their files), (2) the full suite shows no new failures
      relative to the failing run, (3) the Adversarial Verification
      agent fails to break the fix. Results are compared as
      normalised `(file, test name)` pairs, so pytest and unittest
      output both check against the Stage 1 evidence. The adversary
      runs only when the tests are green: a patch the suite already
      killed does not spend model quota, and its failing output is
      the feedback the next attempt needs.
- [x] Adversarial Verification agent: a Gemini call (free tier,
      structured JSON, schema validated in code) briefed with the
      root cause, the patch, and the sandbox results, required to
      make the strongest concrete case that the fix is wrong,
      incomplete, or symptom hiding (hardcoded fixture values,
      swallowed errors, wrong layer, a second call site left broken).
      Verdicts `survives` / `broken`; a `broken` verdict fails the
      gate even with a green suite. A failed or malformed adversary
      call makes the gate `inconclusive`, which never ships and
      routes exactly like a failure.
- [x] Retry loop: a failed or inconclusive gate records its evidence
      in `verification_notes` (plus the structured `adversary`
      report), and the Repair prompt carries that feedback, including
      the adversary's attack, into the next attempt. Attempts are
      capped by the Mission Controller (default 2 repair attempts);
      exhaustion escalates to a human with the full audit trail. A
      fix is marked `verified` only when all three conditions held
      in the same attempt; partial conditions never verify.
- [x] Gate reachability: an incident whose patch was proposed in an
      earlier run resumes at the gate when it carries a local
      checkout (`repo_path`). Without a checkout the gate cannot run
      and the incident stays held at `awaiting_verification` with a
      `gate_pending` audit event: not trusted, not shipped.
- [x] Tests, all offline (`tests/test_stage3.py`): a real fixture
      project (`tests/fixtures/project`, a shop whose checkout calls
      a renamed pricing function) in a throwaway git repo, recorded
      Gemini responses at the transport seam. Covered: a correct fix
      passing all three conditions to `verified`; a fix that heals
      the failing test but breaks another test, sent back on the
      suite evidence alone; a green but symptom hiding fix (swallowed
      error, hardcoded total) broken by the adversary; an unavailable
      adversary yielding `inconclusive`, never `verified`; the
      adversary break looping back to a second repair attempt that
      verifies; escalation with the evidence attached when attempts
      run out; and sandbox isolation (the original checkout is
      byte-identical afterwards, no stray worktrees).

## Stage 4: Pull requests, demo, launch — BUILT (live run pending two account steps)

- [x] Controller opens a PR from the verified patch only
      (`sentinel_ci/pull_request.py`, driven by
      `sentinel_ci/pipeline.py`): the branch
      `sentinel-ci/run-<run_id>` is created at the failing commit and
      the patch is committed through the Git data API (blob, tree,
      commit, ref), applied to the file contents fetched at that
      commit by a strict hunk applier that refuses a context
      mismatch rather than guessing. The PR body is the audit trail:
      failure summary, hypothesis and confirmed root cause with its
      confidence, the diff, the three verification gate results
      including the adversary's strongest attack and why the fix
      survived it, the attempt history, and the full audit trail.
      Mid range root cause confidence (below 0.85) opens the PR as a
      draft. Never auto merge, stated in the PR body itself. A
      publishing failure leaves the incident `verified` with the
      failure recorded: the fix is proven, the publishing failed,
      and a human can open the PR by hand from the same evidence.
- [x] `action.yml` published flow: the composite Action runs the
      full pipeline (`sentinel-ci heal`) when a consumer
      `workflow_run` failure event fires it: it checks the failed
      run's commit out for the verification gate, reads
      `GEMINI_API_KEY` from the consumer repo secret, and fails
      gracefully with one clear log line when the key is missing
      (exit 0, no changes). Tagged `v1`, so the consumer reference
      `KcDatha/sentinel-ci@v1` in `examples/sentinel-ci.yml`
      resolves. Marketplace listing draft not done; the README
      install section is the listing copy for now.
- [x] Demo repository: `KcDatha/sentinel-ci-demo`, a small Python
      shop with a real unittest suite and three planted, recognizable
      failures committed in sequence (a function renamed in pricing
      whose cart caller was not updated, a receipt footer with a
      hardcoded year against a test that derives the current year,
      and an order JSON key renamed away from the documented
      contract). The suite is green at the first commit and gains
      exactly one failure per plant, verified locally at every
      commit. The consumer workflow files are written and ready but
      NOT yet in the repo: GitHub rejected the push because the
      build environment's OAuth token (scopes `gist`, `read:org`,
      `repo`) lacks the `workflow` scope that files under
      `.github/workflows` require, and SSH is blocked in the
      sandbox. Guru lands them with one step: `gh auth refresh
      -h github.com -s workflow` and a push of the ready files, or
      adding both files in the web interface. Until then no CI has
      run in the demo repo; the red build is verified locally, not
      on GitHub.
- [ ] A recorded end to end healing run (red build, diagnosis,
      repair, verifier rejecting an attempt, verified fix, PR).
      Blocked on two account owner steps, in order: the workflow
      files above, and the `GEMINI_API_KEY` secret (no key is
      available in the development environment; the variable is set
      but empty, and no secret was invented or set). Guru adds a
      free Google AI Studio key under the demo repo's Settings >
      Secrets and variables > Actions, and the next failed CI run
      (or a re-run of the current one) triggers the heal. Until
      then the Action logs the missing key and changes nothing.
- [x] Live demo page: `demo/` reflects Stages 1 to 4 (all six agents
      working, Stage 4 landing note, demo repo linked), design
      tokens unchanged; the Vercel site is GitHub connected and
      redeploys on push.
- [ ] Launch package: demo GIF or short screen recording, LinkedIn
      post draft for Guru's approval before posting. (The recording
      wants the live run above; the post needs Guru's permission,
      per his standing rule.)

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
