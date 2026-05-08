"""
features/github_repo_analyzer.py
==================================
Deep GitHub repository analysis for the Crucible pipeline.

Fetches structured context from a public (or authenticated) GitHub repository
and produces a condensed ``ResearchContext`` supplement that Stage 0 / Stage 1
agents can use to ground their analysis in real-world project signals.

Data collected
--------------
* **README** — primary documentation (first 6 000 chars)
* **Recent issues** — titles + labels + state (up to 25)
* **Recent closed PRs** — titles + merge status (up to 15)
* **Latest commits** — short SHA + message (up to 20)
* **Repository metadata** — stars, forks, open issues, language, topics

Authentication
--------------
Set ``GITHUB_TOKEN`` (or ``GH_TOKEN``) in the environment for private repos
and to avoid the unauthenticated 60 req/h rate limit.

Transport
---------
Uses ``httpx`` (already a project dependency) with:
- ``GITHUB_ANALYZER_TIMEOUT`` env var (default 15 s)
- ``GITHUB_ANALYZER_MAX_RETRIES`` env var (default 2)
- Exponential back-off on 5xx / rate-limit responses

Usage::

    from crucible.features.github_repo_analyzer import (
        analyze_github_repo,
        GitHubRepoConfig,
    )

    config = GitHubRepoConfig(owner="openai", repo="openai-python")
    result = analyze_github_repo(config)
    print(result.context_text[:800])
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# httpx is a required project dependency (requirements.txt)
try:
    import httpx as _httpx
    _HAS_HTTPX = True
except ImportError:  # pragma: no cover
    _httpx = None  # type: ignore[assignment]
    _HAS_HTTPX = False

# ── Configuration ─────────────────────────────────────────────────────────────

_GITHUB_API = "https://api.github.com"

try:
    from .. import _env
except ImportError:  # pragma: no cover - script-mode fallback
    import _env  # type: ignore[no-redef]


def _env_int(name: str, default: int) -> int:
    return _env.env_int(name, default)

def _env_float(name: str, default: float) -> float:
    return _env.env_float(name, default)

_TIMEOUT: float = _env_float("GITHUB_ANALYZER_TIMEOUT", 15.0)
_MAX_RETRIES: int = _env_int("GITHUB_ANALYZER_MAX_RETRIES", 2)


def _env_float_gh(name: str, default: float) -> float:
    return _env.env_float(name, default, clamp_min=0.0)


_CACHE_TTL: float = _env_float_gh("GITHUB_ANALYZER_CACHE_TTL", 3600.0)

# In-memory cache: key -> (GitHubRepoResult, expires_at_epoch)
_MEMORY_CACHE: Dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(config: "GitHubRepoConfig") -> str:
    raw = (
        f"{config.owner}/{config.repo}:{config.readme_max_chars}:"
        f"{config.max_issues}:{config.max_prs}:{config.max_commits}:"
        f"{config.include_closed_issues}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_get(key: str) -> Optional["GitHubRepoResult"]:
    """Return cached result if still within TTL, else None."""
    if _CACHE_TTL <= 0:
        return None
    with _CACHE_LOCK:
        entry = _MEMORY_CACHE.get(key)
    if entry is None:
        return None
    result, expires_at = entry
    if time.time() > expires_at:
        with _CACHE_LOCK:
            _MEMORY_CACHE.pop(key, None)
        return None
    return result


def _cache_set(key: str, result: "GitHubRepoResult") -> None:
    """Store result in in-memory cache."""
    if _CACHE_TTL <= 0:
        return
    expires_at = time.time() + _CACHE_TTL
    with _CACHE_LOCK:
        _MEMORY_CACHE[key] = (result, expires_at)


def clear_cache() -> None:
    """Clear the in-memory GitHub API response cache."""
    with _CACHE_LOCK:
        _MEMORY_CACHE.clear()


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class GitHubRepoConfig:
    """Configuration for analysing one GitHub repository."""

    owner: str
    repo: str
    # Max characters to include from README
    readme_max_chars: int = 6_000
    # Max issues / PRs / commits to fetch
    max_issues: int = 25
    max_prs: int = 15
    max_commits: int = 20
    # Include closed issues (in addition to open)
    include_closed_issues: bool = False


@dataclass
class GitHubRepoResult:
    """Structured result of a GitHub repository analysis."""

    owner: str
    repo: str
    # Raw metadata
    stars: Optional[int] = None
    forks: Optional[int] = None
    open_issues_count: Optional[int] = None
    primary_language: Optional[str] = None
    topics: List[str] = field(default_factory=list)
    description: Optional[str] = None
    # Content
    readme_text: str = ""
    issues: List[Dict[str, Any]] = field(default_factory=list)
    pull_requests: List[Dict[str, Any]] = field(default_factory=list)
    commits: List[Dict[str, Any]] = field(default_factory=list)
    # Output
    context_text: str = ""
    errors: List[str] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


# ── HTTP client helpers ───────────────────────────────────────────────────────

def _build_headers() -> Dict[str, str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    headers: Dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Crucible-Crew/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(
    client: Any,
    url: str,
    params: Optional[Dict[str, Any]] = None,
) -> Optional[Any]:
    """
    GET *url* with retry logic.  Returns parsed JSON or None on error.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.get(url, params=params, timeout=_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (403, 429):
                # Rate-limited — wait and retry
                try:
                    retry_after = int(resp.headers.get("Retry-After", "10"))
                except (ValueError, TypeError):
                    retry_after = 10
                if attempt < _MAX_RETRIES:
                    time.sleep(min(retry_after, 30))
                    continue
                return None
            if resp.status_code >= 500 and attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception:
            if attempt < _MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            return None
    return None


# ── Fetchers ──────────────────────────────────────────────────────────────────

def _fetch_repo_meta(
    client: Any, owner: str, repo: str
) -> Optional[Dict[str, Any]]:
    return _get_json(client, f"{_GITHUB_API}/repos/{owner}/{repo}")


def _fetch_readme(
    client: Any, owner: str, repo: str, max_chars: int
) -> str:
    data = _get_json(client, f"{_GITHUB_API}/repos/{owner}/{repo}/readme")
    if not isinstance(data, dict):
        return ""
    import base64
    content_b64 = data.get("content", "")
    if not content_b64:
        return ""
    try:
        raw = base64.b64decode(content_b64.replace("\n", "")).decode("utf-8", errors="replace")
        return raw[:max_chars]
    except Exception:
        return ""


def _fetch_issues(
    client: Any,
    owner: str,
    repo: str,
    max_issues: int,
    include_closed: bool,
) -> List[Dict[str, Any]]:
    state = "all" if include_closed else "open"
    data = _get_json(
        client,
        f"{_GITHUB_API}/repos/{owner}/{repo}/issues",
        params={"state": state, "per_page": min(max_issues, 100), "sort": "updated"},
    )
    if not isinstance(data, list):
        return []
    results = []
    for item in data[:max_issues]:
        if not isinstance(item, dict):
            continue
        # Skip pull requests (GitHub issues API returns both)
        if item.get("pull_request"):
            continue
        labels = [
            lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
            for lbl in (item.get("labels") or [])
        ]
        results.append({
            "number": item.get("number"),
            "title": item.get("title", ""),
            "state": item.get("state", ""),
            "labels": labels,
            "comments": item.get("comments", 0),
        })
    return results


def _fetch_pull_requests(
    client: Any, owner: str, repo: str, max_prs: int
) -> List[Dict[str, Any]]:
    data = _get_json(
        client,
        f"{_GITHUB_API}/repos/{owner}/{repo}/pulls",
        params={"state": "closed", "per_page": min(max_prs, 100), "sort": "updated"},
    )
    if not isinstance(data, list):
        return []
    results = []
    for item in data[:max_prs]:
        if not isinstance(item, dict):
            continue
        results.append({
            "number": item.get("number"),
            "title": item.get("title", ""),
            "merged": item.get("merged_at") is not None,
            "state": item.get("state", ""),
        })
    return results


def _fetch_commits(
    client: Any, owner: str, repo: str, max_commits: int
) -> List[Dict[str, Any]]:
    data = _get_json(
        client,
        f"{_GITHUB_API}/repos/{owner}/{repo}/commits",
        params={"per_page": min(max_commits, 100)},
    )
    if not isinstance(data, list):
        return []
    results = []
    for item in data[:max_commits]:
        if not isinstance(item, dict):
            continue
        sha = (item.get("sha") or "")[:7]
        commit = item.get("commit") or {}
        message_full = str(commit.get("message") or "")
        # First line only
        message = message_full.split("\n")[0][:120]
        results.append({"sha": sha, "message": message})
    return results


# ── Context builder ───────────────────────────────────────────────────────────

def _build_context(result: GitHubRepoResult) -> str:
    lines: List[str] = [
        f"=== GitHub Repository Context: {result.full_name} ===",
    ]

    if result.description:
        lines.append(f"Description: {result.description}")

    meta_parts: List[str] = []
    if result.stars is not None:
        meta_parts.append(f"⭐ {result.stars:,}")
    if result.forks is not None:
        meta_parts.append(f"🍴 {result.forks:,} forks")
    if result.open_issues_count is not None:
        meta_parts.append(f"{result.open_issues_count} open issues")
    if result.primary_language:
        meta_parts.append(f"Language: {result.primary_language}")
    if meta_parts:
        lines.append("  ".join(meta_parts))

    if result.topics:
        lines.append(f"Topics: {', '.join(result.topics[:10])}")

    if result.readme_text.strip():
        lines.append("")
        lines.append("--- README (excerpt) ---")
        lines.append(result.readme_text.strip())
        lines.append("--- End README ---")

    if result.issues:
        lines.append("")
        lines.append(f"--- Recent Issues ({len(result.issues)}) ---")
        for iss in result.issues[:15]:
            labels_str = f"  [{', '.join(iss['labels'][:3])}]" if iss["labels"] else ""
            state_icon = "✓" if iss["state"] == "closed" else "○"
            lines.append(f"  {state_icon} #{iss['number']}: {iss['title']}{labels_str}")

    if result.pull_requests:
        lines.append("")
        lines.append(f"--- Recent Merged PRs ({len(result.pull_requests)}) ---")
        for pr in result.pull_requests[:10]:
            merged_icon = "✓ MERGED" if pr["merged"] else "✗ CLOSED"
            lines.append(f"  [{merged_icon}] #{pr['number']}: {pr['title']}")

    if result.commits:
        lines.append("")
        lines.append(f"--- Recent Commits ({len(result.commits)}) ---")
        for commit in result.commits[:10]:
            lines.append(f"  {commit['sha']} {commit['message']}")

    lines.append("=== End GitHub Context ===")
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────────────

def analyze_github_repo(config: GitHubRepoConfig) -> GitHubRepoResult:
    """
    Fetch and structure context from a GitHub repository.

    Parameters
    ----------
    config:
        ``GitHubRepoConfig`` specifying the target repository and fetch limits.

    Returns
    -------
    GitHubRepoResult
        Contains metadata, issues, PRs, commits, and a ready-to-inject
        ``context_text`` string.
    """
    cache_key = _cache_key(config)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    result = GitHubRepoResult(owner=config.owner, repo=config.repo)

    if not _HAS_HTTPX or _httpx is None:
        result.errors.append(
            "github_repo_analyzer: httpx not available (this should not happen "
            "since httpx is a required project dependency)."
        )
        return result

    headers = _build_headers()
    client = None
    client = _httpx.Client(headers=headers)

    try:
        # Repository metadata
        meta = _fetch_repo_meta(client, config.owner, config.repo)
        if meta is None:
            result.errors.append(
                f"Could not fetch repository metadata for {config.owner}/{config.repo}. "
                "Check that the repo exists and your GITHUB_TOKEN is valid."
            )
            return result

        result.stars = meta.get("stargazers_count")
        result.forks = meta.get("forks_count")
        result.open_issues_count = meta.get("open_issues_count")
        result.primary_language = meta.get("language")
        result.description = meta.get("description")
        result.topics = list(meta.get("topics") or [])

        # README
        result.readme_text = _fetch_readme(
            client, config.owner, config.repo, config.readme_max_chars
        )

        # Issues
        result.issues = _fetch_issues(
            client,
            config.owner,
            config.repo,
            config.max_issues,
            config.include_closed_issues,
        )

        # Pull requests
        result.pull_requests = _fetch_pull_requests(
            client, config.owner, config.repo, config.max_prs
        )

        # Commits
        result.commits = _fetch_commits(
            client, config.owner, config.repo, config.max_commits
        )

    except Exception as exc:
        result.errors.append(f"Unexpected error during GitHub analysis: {exc}")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    result.context_text = _build_context(result)
    _cache_set(cache_key, result)
    return result


def analyze_github_repo_from_url(url: str) -> GitHubRepoResult:
    """
    Parse a GitHub repository URL and delegate to ``analyze_github_repo``.

    Supports formats:
    - ``https://github.com/owner/repo``
    - ``https://github.com/owner/repo.git``
    - ``git@github.com:owner/repo.git``
    """
    import re
    patterns = [
        r"github\.com[:/]([^/]+)/([^/.]+?)(?:\.git)?$",
        r"github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/|$)",
    ]
    owner, repo = "", ""
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            owner, repo = m.group(1), m.group(2)
            break

    if not owner or not repo:
        empty = GitHubRepoResult(owner="", repo="")
        empty.errors.append(f"Could not parse GitHub owner/repo from URL: {url!r}")
        return empty

    config = GitHubRepoConfig(owner=owner, repo=repo)
    return analyze_github_repo(config)
