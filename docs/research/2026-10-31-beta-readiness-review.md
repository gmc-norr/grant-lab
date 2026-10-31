**Grant Lab beta readiness review — 31 October 2026**

Reviewed revision: `5bd6b112`. Application code and deployment configuration were left unchanged.

The match edition has a strong automated baseline and its main user journeys work against a real, disposable backend. I recommend a focused correction pass before inviting beta users: prevent notes from crossing profile boundaries, stop stale embeddings from excluding updated calls, finish cancellation handling, and make deadline status respect the actual closing time. Ownership transfer also needs to serialize with ordinary profile edits. A deployment-template security defect and a failing dependency audit should be addressed alongside those changes.

I interpreted the smaller beta UI as `APP_EDITION=match`: calls triage, profiles, boards, account settings and administration. This edition **still uses a React SPA**. The larger proposal-assist cockpit belongs to the full edition; there is no separate server-rendered browser UI in the current tree. `SPA_MODE=disabled` serves the API without a browser UI. The future cockpit was excluded from the beta recommendation, although the complete backend suite was run.

Priorities below mean P1: high-impact exposure under the stated condition; P2: correctness, authorization, or release-verification issue; P3: follow-up improvement. A passing defect probe means the faulty behavior was reproduced, not that the implementation is correct.

**F1 · P1, conditional on deployment · HTTPS accepts the README's public session-signing secret.**

Locations: [README.md:87](../../README.md#L87), [config.py:12](../../src/grant_lab/config.py#L12), [config.py:931](../../src/grant_lab/config.py#L931).

The quickstart supplies `SESSION_SECRET=local-development-secret-change-me`. It is 34 characters long and absent from `_DEV_SECRET_SENTINELS`, so the production validator accepts it after changing `APP_BASE_URL` to HTTPS. Someone who retains this documented value has a public signing key protecting every account.

Executed reproduction: started the match application against disposable PostgreSQL with HTTPS configuration and that exact secret. An independently constructed signer using the public value produced a cookie accepted by `GET /api/admin/users`, returning 200 for the seeded administrator. No password was needed. This demonstrates the consequence for a deployment using the example; it does not establish that the running deployment uses it. The local `.env` has a different secret and meets the production length requirement.

Change: reject this exact documented placeholder, and replace the copyable secret in the quickstart with a generation step. Verify that every documented nonempty example secret is rejected under HTTPS. Keep the existing minimum-length check; length alone cannot identify a published key.

**F2 · P2 · Browser history navigation can save one profile's note onto another profile.**

Locations: [TriageFeed.tsx:654](../../frontend/src/routes/TriageFeed.tsx#L654), [TriageCard.tsx:131](../../frontend/src/routes/TriageCard.tsx#L131), [DecisionNoteStrip.tsx:110](../../frontend/src/routes/DecisionNoteStrip.tsx#L110).

The feed keys each card by call ID, and the note editor also keys only by call ID. When the same saved call appears under two profiles, React retains the editor while its `profileId` changes. The strip intentionally preserves dirty text when a new server seed arrives, but its next save uses the new target.

Executed reproduction in Chromium against the real match bundle and real API:

1. Save the same call under profiles B and A, with distinct notes.
2. Visit B's Saved view and use the profile picker to visit A's Saved view.
3. Open A's note and type a draft without committing it.
4. Use browser Back to return to B. The input still contains A's draft.
5. Press Enter. B's stored note becomes A's draft.

An independent component probe reproduced the same target change. The recent save-serialization fix prevents reversed request completion; it does not prevent this editor-identity error.

Change: include profile ID and decision state in editor/card identity, and bind any pending write to the original decision target. Preserve the recent protection against an editor remounting on its own save acknowledgement. Add a browser Back regression with two profiles sharing a call and different notes.

**F3 · P2 · A stale call embedding can give corrected call content a zero score without consulting the model.**

Locations: [rank_pair.py:441](../../src/grant_lab/ranking/rank_pair.py#L441), [worker.py:217](../../src/grant_lab/ranking/worker.py#L217), [orchestrator.py:291](../../src/grant_lab/ingestion/orchestrator.py#L291).

Ingestion stores changed content before attempting its new embedding. If embedding fails, it preserves the old vector and still queues ranking work. `_safe_cosine()` checks model compatibility but does not check whether either vector describes the current text. The worker defers known-stale profile vectors for up to 24 hours, but has no equivalent call-vector check. The aged-profile fallback also reaches this unchecked cosine.

Executed worker reproduction: supplied a current profile vector, changed a call's text while retaining its old, orthogonal vector and old embedding fingerprint, and drained the queue. The stored score was 0 with reason `embedding`; the model client was never called. Removing the stale call vector let the same content reach the mocked model and score 80. This tests the control flow, not the scientific quality of the model's judgment.

Change: treat a known-mismatched embedding as unavailable for both calls and profiles. Either defer until repaired or take the existing UNKNOWN/LLM path. A bounded deferral must not eventually authorize a hard rejection using a vector already known to be stale. Test the path from a content update through an embedding failure to the ranking result.

**F4 · P2 · A cancellation appearing only in a title leaves an existing call advertised as open.**

Location: [eu_portal.py:714](../../src/grant_lab/sources/eu_portal.py#L714).

The new closed-call update path correctly handles expired rows and identifiers ending in `-CANCELLED`. However, `_normalize()` still returns `None` when an unchanged identifier acquires a title such as `Active topic - CANCELLED`. That removes the cancellation signal before ingestion can update the row already held under that identifier.

Executed reproduction: stored a future call, normalized the same identifier with a cancelled title, and ingested the resulting empty set. Ingestion reported success; the original title and future deadline remained. The dashboard, digest and board queries continue to regard that stored deadline as open.

Change: let an accepted title-only cancellation close an existing row under the same identifier, while retaining the narrow cancellation matcher and avoiding insertion of new cancelled records. If a title match is considered too uncertain to close automatically, surface that uncertainty for review instead of reporting an ordinary successful refresh. The larger problem of calls disappearing entirely from the upstream open set remains separate and unresolved.

This is a residual of the October 29 review's F5, whose expired-call and identifier-suffix cases have been fixed.

**F5 · P2 · Saved calls that closed earlier today are not marked closed.**

Locations: [Deadline.tsx:47](../../frontend/src/routes/Deadline.tsx#L47), [Deadline.tsx:78](../../frontend/src/routes/Deadline.tsx#L78), [TriageCard.tsx:70](../../frontend/src/routes/TriageCard.tsx#L70).

`DeadlineLocal` prefers the calendar-date field and ignores the exact instant supplied as `isoFallback`. It therefore considers today's deadline open until midnight Stockholm time. The backend uses the exact instant, and EU calls now have their real 17:00 Brussels cutoff. Saved and Hidden deliberately retain closed calls, so the inconsistency is visible there: an expired call lacks the Closed label and may still say `today`.

Executed component reproduction: froze time at `2026-10-31T18:00:00Z` and rendered a deadline of `2026-10-31T13:00:00Z`, with local date `2026-10-31`. The result had state `soon`, displayed the date, and contained no Closed label. The board's local filtering likewise uses date-only status while displaying a cached payload.

Change: use the instant to decide whether a call has closed, while continuing to display its Stockholm calendar date. Show the closing time when it helps users act on the final day. Preserve the DST/date-format tests and add same-day before/after-cutoff cases.

**F6 · P2 · An ordinary profile edit can commit after its author has transferred ownership and lost access.**

Locations: [profiles.py:508](../../src/grant_lab/web/api/profiles.py#L508), [profiles.py:523](../../src/grant_lab/web/api/profiles.py#L523).

Ownership/sharing mutations acquire the profile row lock through `_profile_for_edit()`. `update_profile()` instead authorizes against an unlocked `session.get()`. The ownership transfer can commit while that edit remains in flight; the old request then writes using its previously loaded ownership state.

Executed two-request reproduction: paused an ordinary edit just after authorization, transferred the private profile to another active user without retaining read access, then resumed the edit. Both requests returned 200. The outgoing owner's subsequent GET returned 404, but the new owner's profile contained the outgoing owner's post-transfer write.

Change: acquire the same profile row lock before checking authorization and hold it through the update commit. Keep the existing 404/403 distinction. The background embedding/backfill work already runs after the transaction, so it need not extend that lock. Verify that an edit and a transfer have a consistent commit order.

**F7 · P2 · The Python dependency audit currently fails.**

Locations: [uv.lock:940](../../uv.lock#L940), [.github/workflows/ci.yml](../../.github/workflows/ci.yml), [oidc.py:108](../../src/grant_lab/auth/oidc.py#L108).

Both the plain audit and the CI-equivalent command with the existing certifi waiver exited 1: two advisories affect locked `joserfc==1.7.4`, `CVE-2026-119283` and `CVE-2026-119284`. The production JavaScript dependency audit passed. The maintainer describes a [JWE recipient-count CPU-exhaustion issue](https://github.com/authlib/joserfc/security/advisories/GHSA-jpxq-7h8p-2q76) and a [compact-JWS allocation-before-size-validation issue](https://github.com/authlib/joserfc/security/advisories/GHSA-9pg5-8c7x-5hfx).

Reachability matters: this app uses signed JWT decoding for Google OIDC, not the affected JWE decryption API, and obtains the ID token from Google's token endpoint. I did not demonstrate a directly exploitable application path for either advisory. The verified release issue is a failing audit gate, not a demonstrated authentication bypass.

Change: deliberately update the dependency and lock to a version clearing both advisories, regenerate the derived dependency metadata, and rerun the OIDC tests and audit. The active audit feed lists fixes at 1.7.5 and 1.7.6 respectively; check the resulting audit rather than relying only on the maintainer page's version label.

**F8 · P2 operational gap · Source failures have no explicit state in the admin UI.**

Locations: [admin.py:111](../../src/grant_lab/web/api/admin.py#L111), [AdminDashboard.tsx:86](../../frontend/src/routes/AdminDashboard.tsx#L86), [ingestion/alerts.py](../../src/grant_lab/ingestion/alerts.py).

Source failure streaks are persisted, but `SourceOut` exposes only name, slug, last successful fetch time and call count. An operator can infer trouble from an old timestamp, but receives no failed/degraded source status. Email alerts require both an alert recipient and a working transport. Neither is configured in the local `.env` reviewed here.

Change: expose the failure streak and a clear stale/failed state in the admin source table; identify when there has never been a successful fetch. If alerts are part of beta operations, configure and test their delivery. This remains open from the October 6 review's M4; it was rechecked by source inspection, not by provoking a live source outage.

**Additional P3 findings and follow-up work.**

The match process boundary is incomplete after startup. In a fresh interpreter, constructing its scheduler loaded zero proposal-assist modules. Invoking its registered, disabled `coherence_drain` tick loaded **66 proposal-assist modules**, including `claude_agent_sdk` and `pycrdt`, before returning. The import at [scheduler.py:1143](../../src/grant_lab/scheduler.py#L1143) precedes the drafting-disabled check. Auth revocation handlers also import collab code. The HTTP route exclusion still holds; this observation concerns runtime coupling and the limits of the boot-only import test. Gate imports and job registration by edition, while preserving deliberately supported recovery work in a full deployment with drafting disabled.

Keep a real match frontend/backend browser job in CI. The current match browser suite mocks `/api`; the separate backend/browser harness targets the full edition. Today's additional disposable-backend smoke tests filled that gap manually and found F2 through actual browser history. Make those ordinary journeys durable coverage: registration, approval, login, default profile selection, triage, notes across profiles, and logout.

The match build's main JavaScript chunk is **775.18 kB, 239.40 kB gzip**, and Vite emits its large-chunk warning. Route-level lazy loading outside the triage path is a reasonable follow-up. No client-device performance benchmark was run, so this is an optimization opportunity rather than a measured beta blocker.

**Local configuration observations.**

The local `.env` selects `APP_EDITION=match`, `SPA_MODE=prod` and `DRAFTING_ENABLED=false`. Its signing secret differs from the public README example and meets the production length floor. These observations describe the file, not the running service.

It also specifies an HTTP base URL, sets `DIGEST_DRY_RUN=1`, and has no email transport or source-alert recipient. If this file is intended for beta deployment, establish the browser-facing HTTPS origin and corresponding proxy configuration before using real accounts. Digests and email alerts are disabled by this configuration; configure and verify them if they are included in the beta. No credentials, private profile content or production database rows were copied into this report.

Before inviting users, verify the exact match package/backend pairing, migrations through `0085`, single-process startup, proxy and cookie behavior at the external origin, and a restore from a recent backup. These deployment checks were not established by the repository tests.

**Validation performed.**

| Check | Result |
|---|---|
| Complete backend suite, `env -u ANTHROPIC_API_KEY uv run pytest -q -n 16 --dist loadfile` | **6,120 passed, 1 skipped, 6 xfailed**, 163.87 s |
| Backend lint, `uv run ruff check .` | Passed |
| `uv lock --check` | Passed |
| Frontend unit tests | **156 files, 1,326 tests passed** |
| Frontend lint | Passed |
| Match production build, including TypeScript checking | Passed; large-chunk warning |
| Explicit match router-alias test | **1 passed** |
| Match Playwright suite, including its build and bundle-exclusion gate | **34 passed**, 40.1 s |
| Production JavaScript dependency audit | Passed |
| Python audit, including the CI waiver command | Failed: two advisories in `joserfc` |
| Disposable PostgreSQL defect probes | **4 reproduced**: README signing secret, cancelled title, stale vector with control, ownership-transfer race |
| Additional component probes | **2 reproduced**: cross-profile note target, same-day expired deadline |
| Chromium against real match build + disposable backend | Login, save, Saved/detail round trip, calendar control, profile views/edit form, settings, admin pages and board creation passed; no browser exceptions |
| Additional real-backend onboarding flow | Browser registration → admin approval/password issuance → member login → empty-profile screen → logout passed; post-logout `/api/me` returned 401 |
| Real-browser adverse flow | F2 reproduced; profile B's stored note became profile A's uncommitted draft |
| Fresh-interpreter disabled scheduler tick | Proposal-assist import count rose from 0 to 66 |

The six expected failures concern the future collaborative editor's known bridge limitations. The skipped backend test requires a live Anthropic key, which was explicitly removed from the test environment. Defect probes used disposable databases and mocked model clients. The real-backend browser smoke used synthetic data with scheduler execution disabled; scheduler/lifespan behavior was covered separately by the backend suite and targeted import probe. Temporary probe files were removed after the review.

Live Vinnova/EU availability, actual Ollama/model quality and throughput, Google OIDC against a real account, SMTP/Resend delivery, external TLS/proxy behavior, remote CI status, production data, backup restoration and concurrent-user load were **not verified**. The future full-edition browser suite was outside this beta review.

Several earlier issues are now fixed and should not be carried forward as open findings: new profiles explicitly start private; note requests are serialized; out-of-range budgets are rejected at the ingestion boundary; unchanged calls skip embedding work; the test connection-leak warning gate is armed; and the README database URL matches Compose. The new expired-call and identifier-suffix cancellation handling is also present; F4 identifies the remaining title-only case.

Recommended order: close F1's deployment-template hole; fix F2 and F3; complete F4 and F5; serialize F6; clear F7; and establish source monitoring and the actual beta deployment configuration. Preserve the current edition split and defer broad architectural rewrites while collecting beta feedback.
