"""Configuration for SENTINEL CI.

Everything is read from the environment so the same code runs locally,
in tests, and inside the GitHub Action (which maps its inputs and the
consumer repo secrets onto these variables).

Required secrets:
    GEMINI_API_KEY      Google AI Studio key for the Gemini free tier,
                        used by the diagnosis and repair agents (Stage 2+)
    GITHUB_TOKEN        used by the watcher and the pull request stage

SENTINEL CI runs on free tiers only (Gemini free tier, GitHub free,
GitHub Actions free minutes, Vercel free for the demo page), so model
choice defaults to a free tier Gemini model and stays overridable.

Nothing in this module raises at import time: a missing key is reported
by :meth:`Config.problems` so the CLI can print a clear message instead
of a traceback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_MODEL = "gemini-2.5-flash"


@dataclass
class Config:
    """Runtime configuration for one SENTINEL CI invocation."""

    gemini_api_key: str | None = None
    github_token: str | None = None

    # owner/name of the repository to watch and heal.
    repo: str | None = None

    # Model used by the LLM backed agents. A current Gemini free tier
    # model by default; overridable because model choice is a cost,
    # quota, and quality decision for the operator.
    model: str = DEFAULT_MODEL

    # Watcher behaviour.
    poll_interval_seconds: int = 60

    # Verification gate behaviour (Stage 3). A root cause below this
    # confidence never reaches the Repair agent; the run escalates to a
    # human with the evidence trail attached, per the SENTINEL pattern.
    confidence_threshold: float = 0.7
    max_repair_attempts: int = 2

    # Diagnosis behaviour (Stage 2). When the Root Cause agent rejects
    # a hypothesis, the graph routes back for another hypothesis
    # attempt; this caps how many attempts one incident gets before it
    # escalates to a human instead of looping on guesses.
    max_hypothesis_attempts: int = 2

    # Sandbox behaviour (Stage 3). Verification checks the incident
    # repo out at the failing commit into an isolated sandbox (Docker
    # when available, else a temporary git worktree), applies the
    # proposed patch there, and runs the project tests. The original
    # checkout is never mutated. An empty test command means
    # autodetect from the project (pytest when it is the project's
    # runner, else unittest discovery).
    test_command: str = ""
    verify_timeout_seconds: int = 600
    sandbox_image: str = "python:3.12-slim"

    # Directory verification runs execute in (Stage 3, Docker).
    work_dir: str = field(default=".sentinel-ci-work")

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        env = os.environ if env is None else env
        return cls(
            gemini_api_key=env.get("GEMINI_API_KEY") or None,
            github_token=env.get("GITHUB_TOKEN") or None,
            repo=env.get("SENTINEL_CI_REPO") or env.get("GITHUB_REPOSITORY") or None,
            model=env.get("SENTINEL_CI_MODEL", DEFAULT_MODEL),
            poll_interval_seconds=int(env.get("SENTINEL_CI_POLL_INTERVAL", "60")),
            confidence_threshold=float(env.get("SENTINEL_CI_CONFIDENCE_THRESHOLD", "0.7")),
            max_repair_attempts=int(env.get("SENTINEL_CI_MAX_REPAIR_ATTEMPTS", "2")),
            max_hypothesis_attempts=int(env.get("SENTINEL_CI_MAX_HYPOTHESIS_ATTEMPTS", "2")),
            test_command=env.get("SENTINEL_CI_TEST_COMMAND", ""),
            verify_timeout_seconds=int(env.get("SENTINEL_CI_VERIFY_TIMEOUT", "600")),
            sandbox_image=env.get("SENTINEL_CI_SANDBOX_IMAGE", "python:3.12-slim"),
        )

    def problems(self, need_llm: bool = False, need_github: bool = True) -> list[str]:
        """Human readable list of what is missing for the requested mode."""
        out: list[str] = []
        if need_github and not self.github_token:
            out.append("GITHUB_TOKEN is not set")
        if need_github and not self.repo:
            out.append("no repository configured (set SENTINEL_CI_REPO=owner/name)")
        if need_llm and not self.gemini_api_key:
            out.append("GEMINI_API_KEY is not set")
        return out
