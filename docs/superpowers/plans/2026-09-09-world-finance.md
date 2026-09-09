# World Finance Daily Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Deliver an isolated, non-AI international and financial news digest with safe preview and opt-in mail.

**Architecture:** Reuse FeedCollector, RawItem, RetryingClient, QQMailer and StateStore. Add domain-specific selection/rendering and an isolated delivery coordinator. A new default-branch workflow checks out a fixed world-finance branch SHA, with separate read-only preview and environment-gated delivery jobs.

**Tech Stack:** Python 3.12, existing locked dependencies, pytest, GitHub Actions.

**Spec:** docs/world-finance-design.md

## Execution status (2026-09-09)

Content, rendering, isolated state, dedicated-recipient behavior and branch workflow are implemented. Local suite: 445 passed, 1 live test deselected. GitHub branch preview run 34336661040 passed and produced 8 items (6 world, 2 finance), from BBC, Guardian and UN; all seven feeds parsed, five returned eligible-window entries. No email was sent.

Launcher-only master commit f3268be is installed; its CI run 34336965980 passed. Default-branch repository_dispatch preview run 34337014580 also passed with sending skipped. Environment world-finance-production exists and allows only master deployments; WORLD_FINANCE_SCHEDULE_ENABLED=false; no secrets have been copied from the AI edition.

Remaining deployment: the user must fill the four independent environment secrets before a mail test. End-to-end authenticated state push and SMTP delivery are not yet verified for this edition. Do not mark the send/configuration acceptance steps complete until they are actually run.

## Global Constraints

- English source text retained; no AI calls, investment advice, or GitHub ranking.
- At most 12 items, 6 per section, previous 24 hours, no future timestamps.
- Only world-finance-production recipients; never automatically include sender.
- Only world-state/state.json is durable; commit reservation before SMTP, preserve ambiguity after SMTP errors.
- Schedule 06:37 and 07:17 Asia/Shanghai, disabled by default.
- Master receives only the new launcher; existing workflows and AI configuration stay unchanged.

### Task 1: Content and rendering

Files: config/world-sources.yaml, src/ai_daily/world_news.py, tests/test_world_news.py.
Interfaces: select_news(items, now) returns selected RawItems; render_world(items, now, diagnostics) returns RenderedDigest.

- [ ] Write tests with old/future/duplicate/off-topic items and more than 6 items per section; assert selected URLs and section counts.
- [ ] Run `python -m pytest tests/test_world_news.py` and confirm missing behavior.
- [ ] Implement UTC cutoff, category keyword scoring, source-aware deduplication, source diversity, safe escaped HTML and short original excerpts.
- [ ] Add the seven verified feeds; use existing FeedCollector for dated parsing.
- [ ] Run the tests, Ruff and local preview.

### Task 2: Isolated delivery

Files: src/ai_daily/world_finance.py, tests/test_world_delivery.py.
Interfaces: deliver(root, items, now, diagnostics, mailer, checkpoint) uses StateStore(root / 'world-state'); checkpoint commits only that state path to the world branch.

- [ ] Write tests recording state at checkpoint and SMTP boundaries. Assert reserved checkpoint precedes sending, accepted state follows, failed reservation push prevents mail, and failed SMTP remains ambiguous.
- [ ] Run tests before implementation; implement coordinator with existing delivery_operation lock.
- [ ] Require at least two publishers among selected items; preview always has no SMTP side effects.
- [ ] Validate QQ credentials locally before reservation, require explicit world environment in send mode, and never call QQMailer.from_environment (which adds sender).
- [ ] Run focused tests and the complete offline suite.

### Task 3: Workflow and deployment

Files: .github/workflows/world-finance.yml, README.md on world branch only.

- [ ] Configure branch-push preview and default-branch schedule/manual launcher. Read-only preview exports checked out SHA, sends no secrets, uploads world-preview artifacts.
- [ ] Sending job checks out the same SHA, uses dedicated environment and serial delivery concurrency. Preserve repository safety policy: built-in token read-only, checkout credentials not persisted, WORLD_FINANCE_STATE_TOKEN only used for an ephemeral authenticated push to world-finance-daily. Manual entry uses repository_dispatch from master, not workflow_dispatch.
- [ ] Document independent environment and MAIL_TO, visible recipient list, manual preview/send and disabled schedule switch.
- [ ] Push world branch and verify actual GitHub preview, including per-source outcomes and rendered artifact.
- [ ] Add only the launcher to master after preview succeeds. Do not copy existing AI secrets or send without the new recipient configuration.
- [ ] Report preview results and exact user configuration steps; mark pending mail setup honestly.
