#!/usr/bin/env python3
"""
find_initial_commit.py

Find the user who made the *initial* (first / oldest) commit to one or more
GitHub repositories.

The repositories are read from a text file (default: repo_list.txt), one repo
per line. Each line may be any of the following forms:

    https://github.com/owner/repo
    https://github.com/owner/repo.git
    git@github.com:owner/repo.git
    owner/repo
    https://github.example.com/owner/repo         (GitHub Enterprise)

Authentication uses a GitHub Personal Access Token (PAT) with read access.
Provide it via the GITHUB_TOKEN or GITHUB_PAT environment variable, or with
the --token flag.

EFFICIENCY / THROTTLING
-----------------------
Finding the first commit does NOT require walking every commit. GitHub returns
a ``Link`` header when paginating. By requesting ``per_page=1`` we ask for a
single commit per page; the ``rel="last"`` link in the ``Link`` header then
points directly at the page holding the *oldest* commit. So each repository
costs at most TWO API calls regardless of how many commits it has.

To stay well clear of throttling / rate limiting the script:
  * Reads the ``X-RateLimit-Remaining`` / ``X-RateLimit-Reset`` headers after
    every call and sleeps until the window resets when the remaining budget
    drops below a configurable threshold (primary rate limit).
  * Honours ``Retry-After`` and the documented "secondary rate limit" response
    by backing off for exactly as long as GitHub asks (secondary rate limit).
  * Uses exponential backoff on transient network errors and 5xx responses.
  * Processes repositories sequentially (no concurrency) which is the single
    most effective way to avoid GitHub's abuse / secondary rate limits.

Usage
-----
    export GITHUB_TOKEN=ghp_xxx
    python3 find_initial_commit.py                       # reads repo_list.txt
    python3 find_initial_commit.py -i repos.txt -o out.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from typing import Optional
from urllib.parse import urlparse, parse_qs

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "This script requires the 'requests' library.\n"
        "Install it with:  pip install requests\n"
    )
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    input: str
    owner: str = ""
    repo: str = ""
    default_branch: str = ""     # main / master / other
    initial_commit_sha: str = ""
    author_login: str = ""       # GitHub account login of initial committer
    author_name: str = ""        # git author name recorded in the commit
    author_email: str = ""       # git author email recorded in the commit
    authored_date: str = ""      # ISO-8601 date of the initial commit
    last_commit_date: str = ""   # ISO-8601 date of the latest commit
    status: str = ""             # "ok" or an error description


# --------------------------------------------------------------------------- #
# Repo URL parsing
# --------------------------------------------------------------------------- #
_SCP_LIKE = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/](?P<path>.+?)(?:\.git)?/?$")


def parse_repo(raw: str) -> tuple[str, str, str]:
    """Return (host, owner, repo) for a repository reference.

    Raises ValueError if the reference cannot be parsed.
    """
    ref = raw.strip()
    if not ref:
        raise ValueError("empty reference")

    # git@host:owner/repo(.git)  or  ssh://git@host/owner/repo(.git)
    m = _SCP_LIKE.match(ref)
    if m:
        host = m.group(1)
        path = m.group("path")
    elif "://" in ref:
        parsed = urlparse(ref)
        host = parsed.netloc
        # strip credentials such as user@host if present
        if "@" in host:
            host = host.split("@", 1)[1]
        path = parsed.path.lstrip("/")
    else:
        # bare "owner/repo"
        host = "github.com"
        path = ref

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"cannot extract owner/repo from {raw!r}")

    owner, repo = parts[0], parts[1]
    if not host:
        host = "github.com"
    return host, owner, repo


def api_base_for_host(host: str) -> str:
    """Return the REST API base URL for a given GitHub host."""
    host = host.lower()
    if host in ("github.com", "www.github.com"):
        return "https://api.github.com"
    # GitHub Enterprise Server
    return f"https://{host}/api/v3"


def _last_page_from_link_header(link_header: str) -> Optional[int]:
    """Extract the page number of the rel="last" entry from a Link header."""
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        rel = section[1].strip()
        if rel == 'rel="last"':
            qs = parse_qs(urlparse(url).query)
            if "page" in qs:
                try:
                    return int(qs["page"][0])
                except (ValueError, IndexError):
                    return None
    return None


# --------------------------------------------------------------------------- #
# HTTP client with rate-limit awareness
# --------------------------------------------------------------------------- #
class GitHubClient:
    def __init__(
        self,
        token: str,
        min_remaining: int = 20,
        delay: float = 0.0,
        max_retries: int = 5,
        timeout: float = 30.0,
        verbose: bool = True,
    ):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "find-initial-commit-script",
            }
        )
        self.min_remaining = min_remaining
        self.delay = delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.verbose = verbose

    def _log(self, msg: str) -> None:
        if self.verbose:
            sys.stderr.write(msg + "\n")
            sys.stderr.flush()

    def _respect_primary_limit(self, resp: requests.Response) -> None:
        """Sleep if the remaining primary rate-limit budget is too low."""
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining is None or reset is None:
            return
        try:
            remaining_i = int(remaining)
            reset_i = int(reset)
        except ValueError:
            return
        if remaining_i <= self.min_remaining:
            wait = max(0, reset_i - int(time.time())) + 2  # +2s safety buffer
            self._log(
                f"  [rate-limit] {remaining_i} requests left; "
                f"sleeping {wait}s until the window resets."
            )
            time.sleep(wait)

    @staticmethod
    def _secondary_backoff(resp: requests.Response, attempt: int) -> float:
        """Return how long to wait for a 403/429 throttle response."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
        # Documented fallback for secondary rate limits with no Retry-After.
        reset = resp.headers.get("X-RateLimit-Reset")
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining == "0" and reset is not None:
            try:
                return max(0, int(reset) - int(time.time())) + 2
            except ValueError:
                pass
        # Exponential backoff as a last resort.
        return min(60.0, 2.0 ** attempt)

    def get(self, url: str, params: Optional[dict] = None) -> requests.Response:
        """GET with retry, primary- and secondary-rate-limit handling."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            if self.delay:
                time.sleep(self.delay)
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                wait = min(60.0, 2.0 ** attempt)
                self._log(f"  [network] {exc}; retrying in {wait:.0f}s "
                          f"(attempt {attempt}/{self.max_retries}).")
                time.sleep(wait)
                continue

            # Secondary / abuse rate limit.
            if resp.status_code in (403, 429):
                is_throttle = (
                    "Retry-After" in resp.headers
                    or resp.headers.get("X-RateLimit-Remaining") == "0"
                    or "secondary rate limit" in resp.text.lower()
                    or "rate limit" in resp.text.lower()
                )
                if is_throttle and attempt < self.max_retries:
                    wait = self._secondary_backoff(resp, attempt)
                    self._log(
                        f"  [throttled] HTTP {resp.status_code}; backing off "
                        f"{wait:.0f}s (attempt {attempt}/{self.max_retries})."
                    )
                    time.sleep(wait)
                    continue

            # Transient server errors.
            if resp.status_code >= 500 and attempt < self.max_retries:
                wait = min(60.0, 2.0 ** attempt)
                self._log(f"  [server {resp.status_code}] retrying in "
                          f"{wait:.0f}s (attempt {attempt}/{self.max_retries}).")
                time.sleep(wait)
                continue

            # Success or a non-retryable error: keep budget healthy and return.
            self._respect_primary_limit(resp)
            return resp

        # Exhausted retries due to network errors.
        raise RuntimeError(f"request to {url} failed after "
                           f"{self.max_retries} attempts: {last_exc}")


# --------------------------------------------------------------------------- #
# Core logic: find the initial commit
# --------------------------------------------------------------------------- #
def get_repo_metadata(client: GitHubClient, api: str, owner: str,
                      repo: str, result: Result) -> bool:
    """Populate default_branch. Returns False (with result.status set) on error."""
    resp = client.get(f"{api}/repos/{owner}/{repo}")
    if resp.status_code == 404:
        result.status = "not found (or token lacks access)"
        return False
    if resp.status_code == 401:
        result.status = "unauthorized (check the PAT)"
        return False
    if resp.status_code != 200:
        result.status = f"error: HTTP {resp.status_code} {resp.text[:120]}"
        return False
    data = resp.json()
    result.default_branch = data.get("default_branch", "") or ""
    return True


def get_initial_commit(client: GitHubClient, host: str, owner: str,
                       repo: str) -> Result:
    result = Result(input=f"{owner}/{repo}", owner=owner, repo=repo)
    api = api_base_for_host(host)

    # 1st call: repository metadata -> default branch.
    if not get_repo_metadata(client, api, owner, repo, result):
        return result

    commits_url = f"{api}/repos/{owner}/{repo}/commits"

    # 2nd call: one commit per page. Page 1 is the *latest* commit and the
    # Link header reveals the last page (which holds the *oldest* commit).
    resp = client.get(commits_url, params={"per_page": 1})

    if resp.status_code == 404:
        result.status = "not found (or token lacks access)"
        return result
    if resp.status_code == 409:
        result.status = "empty repository (no commits)"
        return result
    if resp.status_code == 401:
        result.status = "unauthorized (check the PAT)"
        return result
    if resp.status_code != 200:
        result.status = f"error: HTTP {resp.status_code} {resp.text[:120]}"
        return result

    first_page = resp.json()
    if not isinstance(first_page, list) or not first_page:
        result.status = "no commits found"
        return result

    # Page 1's commit is the most recent -> record the last-commit date.
    latest_meta = (first_page[0].get("commit") or {}).get("author") or {}
    result.last_commit_date = latest_meta.get("date", "") or ""

    # Determine the oldest commit's page from the Link header.
    link = resp.headers.get("Link", "")
    last_page = _last_page_from_link_header(link)

    if last_page and last_page > 1:
        # 2nd call: jump straight to the page holding the oldest commit.
        resp2 = client.get(commits_url,
                           params={"per_page": 1, "page": last_page})
        if resp2.status_code != 200:
            result.status = (f"error fetching oldest commit: "
                             f"HTTP {resp2.status_code}")
            return result
        page = resp2.json()
        if not isinstance(page, list) or not page:
            result.status = "could not read oldest commit"
            return result
        commit = page[0]
    else:
        # Only one commit / one page: the first response already has it.
        commit = first_page[0]

    _fill_from_commit(result, commit)
    result.status = "ok"
    return result


def _fill_from_commit(result: Result, commit: dict) -> None:
    result.initial_commit_sha = commit.get("sha", "")
    commit_meta = commit.get("commit") or {}
    author_meta = commit_meta.get("author") or {}
    result.author_name = author_meta.get("name", "") or ""
    result.author_email = author_meta.get("email", "") or ""
    result.authored_date = author_meta.get("date", "") or ""

    # The GitHub account tied to the commit (may be null for imported history).
    gh_author = commit.get("author")
    if isinstance(gh_author, dict):
        result.author_login = gh_author.get("login", "") or ""


# --------------------------------------------------------------------------- #
# Input / output
# --------------------------------------------------------------------------- #
def read_repo_list(path: str) -> list[str]:
    refs: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            refs.append(line)
    return refs


def write_csv(path: str, results: list[Result]) -> None:
    fieldnames = list(asdict(results[0]).keys()) if results else [
        f.name for f in Result.__dataclass_fields__.values()
    ]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(asdict(r))


def print_table(results: list[Result]) -> None:
    print()
    print(f"{'REPOSITORY':<32} {'BRANCH':<10} {'INITIAL AUTHOR':<22} "
          f"{'FIRST':<11} {'LAST':<11} STATUS")
    print("-" * 110)
    for r in results:
        repo_col = f"{r.owner}/{r.repo}" if r.owner else r.input
        if r.author_login:
            author = r.author_login
            if r.author_name and r.author_name != r.author_login:
                author += f" ({r.author_name})"
        else:
            author = r.author_name or "-"
        first = (r.authored_date or "")[:10]
        last = (r.last_commit_date or "")[:10]
        print(f"{repo_col[:31]:<32} {(r.default_branch or '-')[:9]:<10} "
              f"{author[:21]:<22} {first:<11} {last:<11} {r.status}")
    print()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find who made the initial commit to each GitHub repo."
    )
    parser.add_argument("-i", "--input", default="repo_list.txt",
                        help="File with one repository per line "
                             "(default: repo_list.txt).")
    parser.add_argument("-o", "--output", default="initial_commit_results.csv",
                        help="CSV output path "
                             "(default: initial_commit_results.csv).")
    parser.add_argument("--token", default=None,
                        help="GitHub PAT. Falls back to $GITHUB_TOKEN / "
                             "$GITHUB_PAT.")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait between API calls "
                             "(default: 0). Increase to be extra gentle.")
    parser.add_argument("--min-remaining", type=int, default=20,
                        help="Pause when the primary rate-limit budget drops "
                             "to this value (default: 20).")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress progress messages on stderr.")
    args = parser.parse_args(argv)

    token = args.token or os.environ.get("GITHUB_TOKEN") \
        or os.environ.get("GITHUB_PAT")
    if not token:
        sys.stderr.write(
            "No GitHub token provided. Set GITHUB_TOKEN or GITHUB_PAT, "
            "or pass --token.\n"
        )
        return 2

    if not os.path.exists(args.input):
        sys.stderr.write(f"Input file not found: {args.input}\n")
        return 2

    refs = read_repo_list(args.input)
    if not refs:
        sys.stderr.write(f"No repositories found in {args.input}.\n")
        return 2

    client = GitHubClient(
        token=token,
        min_remaining=args.min_remaining,
        delay=args.delay,
        verbose=not args.quiet,
    )

    results: list[Result] = []
    total = len(refs)
    for idx, ref in enumerate(refs, 1):
        if not args.quiet:
            sys.stderr.write(f"[{idx}/{total}] {ref}\n")
            sys.stderr.flush()
        try:
            host, owner, repo = parse_repo(ref)
        except ValueError as exc:
            results.append(Result(input=ref, status=f"parse error: {exc}"))
            continue
        try:
            results.append(get_initial_commit(client, host, owner, repo))
        except Exception as exc:  # noqa: BLE001 - report and keep going
            results.append(Result(input=ref, owner=owner, repo=repo,
                                  status=f"error: {exc}"))

    print_table(results)

    if args.output:
        write_csv(args.output, results)
        sys.stderr.write(f"Wrote CSV results to {args.output}\n")

    # Non-zero exit if anything failed, so callers can detect problems.
    failed = [r for r in results if r.status != "ok"]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
