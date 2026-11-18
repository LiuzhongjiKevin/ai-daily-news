# Alibaba shared translation rollout — 2026-11-18

This record supersedes the earlier finance-only scope and quota-check blocker. The user authorized continuing verification/deployment and finance branch protection, accepted the remaining billing risk, and instructed us not to check the Alibaba console or ask for secrets again.

Both production editions use Alibaba TranslateGeneral through one protected ledger on `world-finance-daily`, at `world-state/state.json`. The code retains numeric validation, original-text fallback, 12000 UTF-16 units / 40 requests / 60 seconds per run, no HTTP retry or paid fallback, and fresh remote reservation confirmation before each run may consume its fixed manifest. The AI edition uses only titles and up to 220 characters of news summaries/repository descriptions; unmodified source items remain available.

## November initialization

- `limit=300000`, `baseline=100000`, `reserved=100000`, no existing reservations.
- The 100000 baseline is an explicit **policy withholding**, not measured account usage or an estimate claimed to cover all unknown consumption. It reduces this project's new November capacity to 200000 characters, shared by both editions and their probes.
- Alibaba account consumption and shared RAM usage remain unknown. This initialization does not claim zero previous use, known remaining free quota, or zero billing risk. Other account applications, compromised keys and provider pricing are outside the program's budget.
- The user-approved risk decision permits bounded calls despite that uncertainty. No ledger total is lowered, no reservation refunded and no run ID reused. Future months remain disabled until separately initialized; exhaustion keeps original text and does not enable Tencent fallback.

## Deployment and validation

Finance branch protection includes administrators and blocks force pushes/deletions, with normal state pushes allowed. AI production's existing state credential writes the same ledger; credential permissions are not silently expanded. Both workflows use dedicated production environments, fixed reviewed action SHAs and separate no-mail probes. Each probe uses only the public string Hello. and reserves six units before a single request.

Production switches stay disabled until their respective real probe succeeds. Existing sent-date records are retained, and today's emails are not resent. Tests are evidence of local behavior, not a substitute for a real provider result or future scheduled mail.

Tencent secrets remain stored. Legacy branches/debug references must be separately checked before removing any secret. The scheduler's pre-run delay is independent and is not fixed by changing translation providers.
