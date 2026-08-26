# Task 5 Report — GitHub Candidate Discovery, Snapshots, and Seven-Day Top 10

## Status

DONE

## RED evidence

`python -m pytest tests/test_github_rank.py -v` initially stopped during collection because `ai_daily.collectors.github` did not provide `discover_candidates` or `fetch_repo_snapshots`. This confirmed the Task 5 behavior was absent before implementation.

## GREEN evidence

After implementation, focused tests passed:

```text
tests/test_github_rank.py .......
7 passed
```

The tests cover Trending daily/weekly and Search union, historical retention and case-insensitive deduplication, required GitHub headers and bounded metadata requests, complete metadata mapping, malformed responses, archived/fork filtering, seven-day and trial rankings, fallback to the oldest first-week snapshot, clamped losses, deterministic tie ordering, Top 10 limiting, and save-before-prune ordering.

## Changed files

- `src/ai_daily/collectors/github.py` — Trending/Search candidate discovery, required GitHub headers, bounded metadata fetch, and strict snapshot mapping.
- `src/ai_daily/github_rank.py` — rolling ranking, historical snapshot selection, candidate retention helper, and rank/save/prune sequencing.
- `tests/test_github_rank.py` — fixture-driven Task 5 tests.
- `tests/fixtures/github_trending.html`
- `tests/fixtures/github_search.json`
- `tests/fixtures/github_repos.json`

## Verification

```text
python -m pytest tests/test_github_rank.py -v
7 passed

python -m ruff check src/ai_daily/collectors/github.py src/ai_daily/github_rank.py tests/test_github_rank.py
All checks passed

python -m pytest -m "not live" -v
50 passed, 1 deselected

git diff --check
passed
```

## Commit

`6197965186cadff721a0e0b177bc87b5c5babebd` — `feat: rank GitHub projects by rolling star growth`

## Limitations

- GitHub exposes per-repository metadata endpoints rather than a bulk endpoint. The implementation therefore uses deterministic capped batches, with no more than 100 metadata requests per run.
- Task 5 deliberately supplies helpers only; pipeline orchestration and environment configuration are outside this task.
