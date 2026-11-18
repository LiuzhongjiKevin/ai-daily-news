# Alibaba translation with durable budget: implementation plan

> Execute locally with test-driven development; user has authorized implementation. No API calls, secrets, push, production enablement, or mail sends.

Goal: replace world-finance production TMT with opt-in TranslateGeneral and fail-closed monthly reservations; keep original text on translation failure.

Architecture: a fixed manifest of eligible titles and 220-character excerpts; conservative UTF-16 units; at most 40 requests / 12000 units / 60 seconds. An explicitly initialized, versioned monthly ledger in world-state/state.json reserves the whole manifest before any request. Existing ordinary Git push is the concurrency barrier. Verify remote tip and ledger history from an operator-approved anchor; no automatic initialization/refund/rebase. Month determined at actual request time, with a boundary safety margin. A permit exists only in the process that successfully reserved, never restored from disk.

Constraints: 300000 monthly maximum; no Tencent fallback, retry, HTTP debug or source text in ledger/logs. Existing untracked .coverage/world-preview preserved. Current workflow gets no new secrets or enable flag. Use the existing pinned httpx==0.28.1 transport and official RPC v2 HMAC-SHA1 signing, avoiding a new dependency tree.

- [x] Tests first for manifest, strict ledger, monotonic history, remote persistence failure, duplicate run, month rollover and limits; implement world_translation_budget.py and explicit RunState persistence.
- [x] Tests first for official RPC request/signature and response Code/Data validation, numeric guard, safe fallback, timeout single request, no missing-key calls; implement world_alibaba.py.
- [x] Tests first for deliver ordering, remote Git contention, push uncertainty, repeat mail and sanitized subprocess environments; integrate world_finance.py and world_news.py. Update old TMT delivery tests to exercise the replacement contract; AI edition remains unchanged.
- [x] Document manual initialization, trusted anchor/full Git history, no-force-push protection and rollback limits, shared-account quota checks, scoped Environment secrets, deployment review and separately authorized small sample. Full offline pytest: 524 passed, 1 live test deselected; full Ruff and git diff --check passed.

Later authorization permits one minimal public-text live sample and deployment only after safeguards are ready. Secrets names verified; no values read. Deployment remains blocked: actual branch-protection endpoint returns Branch not protected, and shared-account remaining free quota has not been established. No ledger initialized, no real translation or email sent, no push. Candidate workflow maps new credentials only in send and remains disabled without the explicit Environment variable; production is unchanged.

Review focus: stale checkout/rewritten ledger; call crossing Shanghai month boundary; same run with incremented attempt; reservations retained after cancellation; raw provider errors or request signatures escaping into logs. Tests must pin each behavior. Administrative deletion of the ledger and all history cannot be detected from that same repository alone: require protected append-only history and explain this trust boundary rather than claim absolute protection from account-owner rewrites.


## Follow-up: protected no-mail sample and launcher (2026-11-17)

Implemented fixed Hello. probe through the existing history/protection guard, six-unit durable reservation and confirmed checkpoint; no mail/feeds, no retries/refunds/run reuse. Added explicit manual-only workflow input and mutually exclusive probe job, exact preview SHA, existing concurrency and no SMTP secrets. Production switch remains unchanged. Added AI Memory credential stripping from Git child processes and ran tests with only necessary OS environment variables.

Red/green evidence: missing probe module failed before implementation; missing workflow input failed before launcher change; two credential-stripping tests failed before adding the removal. Follow-up tests: 23 probe cases pass; full Python 3.12 suite 547 passed / 1 live deselected; Ruff passes. Master launcher patch checked and reconstructed against local origin/master in a temporary workflow-only fixture. No push, deployment, real translation or mail.
