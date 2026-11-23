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

    def _send_json(self, method: str, path: str, payload: dict) -> dict:
        """POST or PUT a JSON body; the write side of the client (Stage 4)."""
        url = f"{self._api_root}{path}"
        body = json.dumps(payload).encode("utf-8")
        if _HAVE_REQUESTS:
            resp = requests.request(
                method, url, headers=self._headers(), json=payload, timeout=30
            )
            if resp.status_code >= 400:
                raise GitHubError(
                    f"{method} {path} -> {resp.status_code}: {resp.text[:200]}"
                )
            return resp.json() if resp.content else {}
        req = urllib.request.Request(
            url, data=body, headers=self._headers(), method=method
        )
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed host
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}

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

    def get_file_text(self, repo: str, path: str, ref: str) -> str:
        """One file's text at ``ref`` via the contents API (Stage 2).

        The contents API returns small files base64 encoded. Anything
        it will not inline (very large files come back with an empty
        body and encoding "none") raises, and the caller treats the
        file as unavailable evidence rather than guessing at it.
        """
        from urllib.parse import quote

        data = self._get_json(
            f"/repos/{repo}/contents/{quote(path)}", params={"ref": ref}
        )
        if data.get("encoding") != "base64" or not data.get("content"):
            raise GitHubError(f"contents API did not inline {path} at {ref}")
        import base64

        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    # -- Stage 4: branch, commit, and pull request ----------------------
    # The verified patch is published through the Git data API, so no
    # local clone is needed for this stage: blob, tree, commit, ref,
    # then the pull request itself. Nothing here merges anything.

    def get_repo(self, repo: str) -> dict:
        """Repository metadata; the PR stage needs the default branch."""
        return self._get_json(f"/repos/{repo}")

    def get_commit(self, repo: str, sha: str) -> dict:
        """One commit object; carries the tree sha the PR stage builds on."""
        return self._get_json(f"/repos/{repo}/git/commits/{sha}")

    def create_blob(self, repo: str, text: str) -> str:
        """Store one file's new contents as a blob; returns its sha."""
        import base64

        data = self._send_json(
            "POST",
            f"/repos/{repo}/git/blobs",
            {
                "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "encoding": "base64",
            },
        )
        return data["sha"]

    def create_tree(self, repo: str, base_tree: str, entries: list[dict]) -> str:
        """A new tree on top of ``base_tree``; returns its sha.

        Each entry is ``{"path", "mode", "type", "sha"}``; an entry
        with ``sha`` set to None deletes the path.
        """
        data = self._send_json(
            "POST",
            f"/repos/{repo}/git/trees",
            {"base_tree": base_tree, "tree": entries},
        )
        return data["sha"]

    def create_commit(
        self, repo: str, message: str, tree: str, parents: list[str]
    ) -> str:
        """One commit object; returns its sha."""
        data = self._send_json(
            "POST",
            f"/repos/{repo}/git/commits",
            {"message": message, "tree": tree, "parents": parents},
        )
        return data["sha"]

    def create_ref(self, repo: str, ref: str, sha: str) -> dict:
        """Create ``refs/<ref>`` (for example ``heads/<branch>``)."""
        return self._send_json(
            "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/{ref}", "sha": sha}
        )

    def create_pull_request(
        self,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
        draft: bool = False,
    ) -> dict:
        """Open a pull request; returns the API object (has html_url)."""
        return self._send_json(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "body": body, "head": head, "base": base,
             "draft": draft},
        )
