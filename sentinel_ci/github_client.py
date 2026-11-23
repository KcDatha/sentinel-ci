"""A thin GitHub REST client covering only what SENTINEL CI needs.

Kept deliberately small and behind a plain class so the watcher can be
tested against a fake. Uses ``requests`` when available and falls back
to ``urllib`` from the stdlib, so Stage 1 runs anywhere Python does.
"""

from __future__ import annotations

import io
import json
import urllib.request
import zipfile

API_ROOT = "https://api.github.com"

try:  # pragma: no cover - depends on environment
    import requests

    _HAVE_REQUESTS = True
except ImportError:  # pragma: no cover
    requests = None  # type: ignore[assignment]
    _HAVE_REQUESTS = False


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str | None, api_root: str = API_ROOT):
        self._token = token
        self._api_root = api_root.rstrip("/")

    # -- low level -----------------------------------------------------
    def _headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        url = f"{self._api_root}{path}"
        if _HAVE_REQUESTS:
            resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
            if resp.status_code >= 400:
                raise GitHubError(f"GET {path} -> {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        if params:
            from urllib.parse import urlencode

            url = f"{url}?{urlencode(params)}"
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed host
            return json.loads(resp.read().decode("utf-8"))

    def _get_bytes(self, path: str) -> bytes:
        url = f"{self._api_root}{path}"
        if _HAVE_REQUESTS:
            resp = requests.get(
                url, headers=self._headers(), timeout=60, allow_redirects=True
            )
            if resp.status_code >= 400:
                raise GitHubError(f"GET {path} -> {resp.status_code}")
            return resp.content
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed host
            return resp.read()

    # -- SENTINEL CI surface -------------------------------------------
    def list_failed_runs(self, repo: str, per_page: int = 20) -> list[dict]:
        """Recently completed workflow runs on ``repo`` that failed."""
        data = self._get_json(
            f"/repos/{repo}/actions/runs",
            params={"status": "completed", "per_page": per_page},
        )
        return [
            run
            for run in data.get("workflow_runs", [])
            if run.get("conclusion") == "failure"
        ]

    def download_logs_text(self, repo: str, run_id: int) -> str:
        """Full log text for a run. The API serves a zip of per job files;
        they are concatenated in filename order into one text blob."""
        raw = self._get_bytes(f"/repos/{repo}/actions/runs/{run_id}/logs")
        parts: list[str] = []
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for name in sorted(zf.namelist()):
                if name.endswith(".txt"):
                    parts.append(zf.read(name).decode("utf-8", errors="replace"))
        return "\n".join(parts)

    def last_green_sha(self, repo: str, branch: str, before_sha: str) -> str | None:
        """Most recent successful run head sha on ``branch`` (Stage 2 input)."""
        data = self._get_json(
            f"/repos/{repo}/actions/runs",
            params={"branch": branch, "status": "success", "per_page": 10},
        )
        for run in data.get("workflow_runs", []):
            if run.get("head_sha") and run["head_sha"] != before_sha:
                return run["head_sha"]
        return None

    def compare_diff(self, repo: str, base: str, head: str) -> dict:
        """Changed files and patch between two commits (Stage 2 input)."""
        return self._get_json(f"/repos/{repo}/compare/{base}...{head}")
