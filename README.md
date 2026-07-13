# find_initial_commit

Find the user who made the **initial (first / oldest) commit** to each of a list
of GitHub repositories.

## How it works (and why it won't throttle)

Getting the first commit does **not** require walking every commit. GitHub sends
a `Link` header when paginating; by asking for `per_page=1` the `rel="last"`
link points straight at the page holding the oldest commit, while page 1 is the
**latest** commit. So each repo costs **at most 3 API calls** (one for repo
metadata / default branch, two for the commit range), no matter how large it is.

To stay clear of rate limiting the script:

- Reads `X-RateLimit-Remaining` / `X-RateLimit-Reset` after every call and
  pauses until the window resets when the budget runs low (**primary** limit).
- Honours `Retry-After` and GitHub's documented **secondary rate limit**
  response, backing off exactly as long as asked.
- Uses exponential backoff on network errors and 5xx responses.
- Runs **sequentially** (no concurrency) — the most reliable way to avoid
  GitHub's abuse / secondary rate limits.

## Setup

```bash
pip install requests
export GITHUB_TOKEN=ghp_your_pat_here   # PAT with read access to the repos
```

## Usage

```bash
# Reads repo_list.txt, prints a table and writes initial_commit_results.csv:
python3 find_initial_commit.py

# Custom input file and CSV output path:
python3 find_initial_commit.py -i repos.txt -o results.csv

# Be extra gentle (0.5s between calls) and pause earlier:
python3 find_initial_commit.py --delay 0.5 --min-remaining 50
```

### `repo_list.txt` format

One repository per line. Blank lines and `#` comments are ignored. Accepted
forms:

```
https://github.com/owner/repo
https://github.com/owner/repo.git
git@github.com:owner/repo.git
owner/repo
https://github.your-enterprise.com/owner/repo   # GitHub Enterprise Server
```

## Output

A table on stdout plus a CSV (default `initial_commit_results.csv`) with these
columns:

| column | meaning |
| --- | --- |
| `owner`, `repo` | the repository |
| `default_branch` | the repo's default branch (`main`, `master`, or other) |
| `initial_commit_sha` | SHA of the oldest commit |
| `author_login` | GitHub account that made the first commit (blank if history was imported) |
| `author_name`, `author_email` | git author recorded in the first commit |
| `authored_date` | ISO-8601 date of the **first** commit |
| `last_commit_date` | ISO-8601 date of the **latest** commit on the default branch |
| `status` | `ok`, or the reason it failed (not found, empty repo, etc.) |

> Note: `author_login` is the GitHub account tied to the commit. For repos whose
> early history was imported/migrated, GitHub may not have a linked account, in
> which case only the git `author_name` / `author_email` are available.
