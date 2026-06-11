# Product Review Backlog — 2026-06-11

A whole-product UX/product review of call-manager (13-agent fan-out: ten subsystem
readers + two persona walkthroughs + a completeness critic). This document captures
**every theme and finding except the one already turned into a spec**, so they aren't lost.

- **Spec'd & in progress:** Theme 1 (First-run value loop) →
  [specs/2026-06-11-first-run-value-loop-design.md](../specs/2026-06-11-first-run-value-loop-design.md)
- **Coordinate, don't duplicate:** ranking-model internals are under active development
  elsewhere. Items tagged `[ranking-internal]` below should be checked against that work
  before acting; items tagged `[ranking-surface]` are UI/feedback-adjacent and mostly safe.

Each item: the problem, evidence (file:line from the review — re-verify before acting, the
codebase moves), and the suggested direction. Rough impact/effort are the reviewers' estimates,
not measured.

> **Accuracy caveats recorded up front** (from the adversarial pass — do not let these mislead
> prioritization):
> - The "references upload fails with *zero* feedback" finding is **overstated**. A global
>   `htmx:responseError` handler exists in [app.js:88-104](../../../src/call_manager/web/static/app.js#L88-L104);
>   the real bug is *generic* error text plus a form-reset that eats typed input, not a totally
>   silent failure. Severity is a notch lower than first reported.
> - The "ranking a profile takes 3–8 hours" throughput math is a **worst case**. It ignores that
>   most (call × profile) pairs short-circuit on cheap deterministic gates (deadline/budget/TRL/
>   cosine hard-fail) with no LLM call. Measure observed per-job latency before using any ETA to
>   justify scope (e.g. queue-priority work).

---

## Theme 1 — First-run value loop  ✅ SPEC'D

See the linked spec. Summary of what it addresses: synchronous bootstrap that freezes the
browser and risks a 504; profile creation never triggering ranking backfill; the
`publications_fetched/skipped` completeness signal computed but never shown; creating a profile
subscribing nobody to the digest; the dashboard never remembering a profile. Not repeated here.

---

## Theme 2 — Login & digest deep-link reliability  (auth)

The weekly digest is the primary re-engagement channel, and it breaks at the first hop.

- **Magic link consumed on a plain GET.** [auth.py:179-217](../../../src/call_manager/web/routes/auth.py#L179-L217)
  atomically consumes the single-use token on GET. Hospital mail gateways (Microsoft Safe Links /
  Proofpoint / Mimecast) pre-fetch URLs and burn the token before the human clicks → the real
  click gets "400 invalid or expired link." This can break **every** login in a hospital email
  environment. **Fix:** interstitial "Confirm sign-in" page — GET renders a button, POST consumes
  the token. *High impact, low effort. Likely the difference between magic-link auth working at
  all here.*
- **Deep-link / next-URL is discarded.** The 401 handler drops the originally requested path
  ([main.py:296-300](../../../src/call_manager/web/main.py#L296-L300) — comment admits it's "a
  one-line change") and `login_verify` hardcodes redirect to `/`. Every digest click from an
  expired session strands the user on the generic dashboard. **Fix:** thread a signed relative
  next-path through login. *High impact, low effort.*
- **Invalid/expired-token dead end** renders generic `error.html` whose only CTA loops back to
  `/login`. **Fix:** dedicated page with an inline "request a new link" form.
- **Silent failure modes:** wrong-domain / typo'd email shows the same "check your email" success
  page (enumeration-resistant by design, but punishes legit users); rate-limit lockout after 5/15min
  shows success with no "check spam / wait a minute" guidance; the `you@ki.se` placeholder is
  hardcoded, not driven by `ALLOWED_EMAIL_DOMAINS`.
- **Shared-workstation ergonomics:** 30-day session default + "Sign out" is secretly
  sign-out-**everywhere** (bumps `session_epoch`). On shared ward computers the next person is the
  previous researcher for a month. **Fix:** shorter session for this deployment; relabel/redesign
  logout; consider per-device server-side sessions.
- **Phishing-shaped email copy:** bare "Click to sign in (valid 15 min)" with no app context,
  no requested-at time, no "ignore if this wasn't you" — exactly what hospital phishing training
  says to distrust.

---

## Theme 3 — Feed hygiene & triage  (dashboard)

Trust-killers and missing triage affordances in the core daily surface.

- **Expired calls keep stale high scores at the top, styled as urgent.** No deadline filter in the
  feed query ([calls.py:130-133](../../../src/call_manager/web/routes/calls.py#L130-L133)) while the
  digest filters correctly. The `deadline_label` macro marks **negative** days as red
  "deadline-soon" ([calls_index.html:21-23](../../../src/call_manager/web/templates/calls_index.html#L21-L23)).
  The "Open calls" summary counts dead calls. **Fix:** default `deadline IS NULL OR deadline > now()`
  with a "show closed" toggle; render negative days as "Closed"; fix the summary count. *High impact,
  low effort.*
- **No call lifecycle / reaper.** Additive-only ingestion, acknowledged TODO at
  [eu_portal.py:125-129](../../../src/call_manager/sources/eu_portal.py#L125-L129). Nothing marks a
  call closed.
- **Sort control disappears when a profile is selected**
  ([calls_index.html:76-84](../../../src/call_manager/web/templates/calls_index.html#L76-L84)) — so
  "my relevant calls, by deadline" (the group lead's planning view) cannot be produced. **Fix:** keep
  Sort in profile view ("Best match" / "Soonest deadline").
- **No text search at all.** No `q` param, no search box. For an aggregator whose pitch is "all calls
  in one place," there's no way to find a half-remembered call. **Fix:** `q=` ILIKE/pg_trgm box.
- **No save/star ("maybe later").** The only positive action is the heavyweight "Start drafting
  workspace"; the only lightweight action is destructive Hide. **Fix:** `user_call_saves` table
  mirroring `UserCallDismissal`; a "Saved" filter chip.
- **Hide is permanent with no undo,** detail-page only ("Removes it from your feed permanently"),
  and its redirect drops filter/profile/page context. **Fix:** undoable Hide with a "Hidden calls"
  list on /me; per-row Hide on the index; preserve context on redirect.
- **No share / copy-link;** and shared links break at the login wall anyway (Theme 2). "Forward this
  to the postdoc who should apply" is the most common action on a relevant call.
- **Engineer jargon leaks to clinicians:** "Gate that short-circuited the LLM," "Cosine similarity:
  0.873," "Reason code: too_soon." **Fix:** plain-language gate labels; move cosine behind a
  "technical details" expander.
- **Context lost on navigation:** detail backlink hardcoded to `/`; empty-state shows developer
  instructions ("uv run python scripts/seed_dev_data.py").

---

## Theme 4 — Source coverage & freshness  (ingestion)

The review's **#1 existential gap**: the founding spec's own "philosophical point" says adapter
coverage matters more than ranking quality, yet only two adapters exist.

- **Only EU Portal + Vinnova are covered.** For a Swedish hospital researcher the *core* funders are
  Vetenskapsrådet, Forte, Cancerfonden, Hjärt-Lungfonden, Barncancerfonden, regional ALF (and maybe
  NIH) — **none** covered; EU + Vinnova is the periphery. A `CancerfondenAdapter` is even sketched in
  [specs/2026-04-19-full-text-ingestion-design.md](../specs/2026-04-19-full-text-ingestion-design.md).
  **Fix:** build the Swedish medical-funder adapters, Cancerfonden first. The shared extraction
  toolkit + one-entry registry make each cheap. *Highest strategic impact; medium effort each
  (real scraping).*
- **Coverage is opaque to users.** Dashboard says only "Aggregated from 2 sources"; nothing states
  what is **not** watched. Dangerous for a miss-prevention tool — the more it's trusted, the more
  users stop checking Cancerfonden themselves, and the tool *causes* the missed call it exists to
  prevent. **Fix:** a "Sources we watch / not yet covered" page or footer block.
- **No manual "add call by URL".** No create path in `calls.py`. This *also* blocks proposal-assist,
  which hard-requires a Call row ([setup.py:283-287](../../../src/call_manager/proposal_assist/phases/setup.py#L283-L287)).
  **Fix:** an "Add call by URL" form (admin or researcher) that runs the existing extraction +
  embed + enqueue path under a seeded "manual" source. *Cheap stopgap for every uncovered funder;
  unlocks drafting against any call.*
- **Silent source breakage for days-to-forever.** 3-strike alert streak is in-memory
  ([alerts.py:39](../../../src/call_manager/ingestion/alerts.py#L39)) → min 3 days to alert, reset on
  any restart; a registry/DB mismatch raises `LookupError` **before** the alerter
  ([orchestrator.py:112-113](../../../src/call_manager/ingestion/orchestrator.py#L112-L113));
  `ADMIN_ALERT_EMAIL` defaults to None so alerting is silently off. (See Theme 6.)
- **Full text freezes after first scrape** — `needs_body` skips re-scraping any call with non-null
  `full_text`; EU calls get amended (budgets, eligibility) but ranking/drafting run on the day-one
  snapshot forever. **Fix:** the deferred re-scrape policy (`full_text_fetched_at` + re-scrape on
  change/age).
- **No cross-source dedup** (`UNIQUE(source_id, external_id)` only) — harmless today, breaks the
  moment an aggregator source (EUfonder.se) overlaps Horizon.
- **Vinnova rolling calls (no closing date) silently dropped** at ingest
  ([vinnova.py:244-253](../../../src/call_manager/sources/vinnova.py#L244-L253)) with no counter.
- **No persisted ingestion-run history** — "did last night's run work?" needs log-grepping. (See
  Theme 6.)

---

## Theme 5 — Digest actionability & urgency

The retention hook under-delivers and has two silent-miss modes.

- **Short-deadline calls are never emailed to anyone.** Only send is Monday 07:00; assembly filters
  `deadline > now` at send time ([assemble.py:168](../../../src/call_manager/digest/assemble.py#L168)).
  A call ingested Tuesday closing before next Monday dies unannounced — the worst outcome for a grants
  tool. **Fix:** a mid-week "closing soon" alert for newly-ingested short-fuse calls.
- **No deadline reminders.** Once sent, a call enters a 78-week suppression list and is never
  re-surfaced; a call shown two months ago closing in 10 days gets no nudge. **Fix:** a "closing
  soon" digest section that exempts ≤14-day deadlines from suppression.
- **`must_include` flood buries the best content** — up to 25 keyword matches (possibly unscored)
  render *above* the 5 ranked top picks; the promised "+N more" summary is never rendered; the email
  doesn't say *which* keyword matched. **Fix:** cap rendered must-include with a real "+N more" link;
  show the matched keyword; render top-scored first.
- **Bounce auto-disable is a one-way trapdoor** — `digest_disabled_at` is set, never cleared by any
  code; admin sees only a count, not who; user is never told; spec-promised admin notification not
  implemented. (See Theme 6 for the re-enable lever.)
- **Async bounces aren't handled** — only synchronous SMTP/Resend 4xx count; Resend's dominant
  accepted-then-bounced class arrives via webhooks and there's no webhook endpoint. Dead addresses
  keep receiving sends.
- **No List-Unsubscribe header** (deliverability risk for a hospital sending domain) and the
  `<details>`/`<summary>` borderline section degrades in Gmail/Outlook.
- **Email omits the triage fields researchers scan** — funder/source, programme, budget exist on
  `Call` but `DigestCallEntry` carries none; the summary is assembled but never rendered.
- **No cadence/threshold control;** subject-line count overpromises (counts collapsed borderline +
  full must-include cap); due-today renders "(in 0 days)".
- **`Subscription.digest_enabled` is a dead control** — the column exists and is filtered on, but no
  UI can ever set it false (subscribe == digest).

---

## Theme 6 — Admin operability & health visibility

"Every recovery path the admin owns ends at SSH or psql." The admin UI has one button and it's the
dangerous one.

- **Manual levers that don't exist** (all wrap already-safe backends): per-source **"Fetch now"**;
  **"Send digest now"** (`run_digest` is week-idempotent via the `DigestRun` unique claim, so it's
  safe by construction); **"Retry dead-letter jobs"**; **"Re-enable digest user"** (clear
  `digest_disabled_at`). **Fix:** four POST routes. *This alone removes every routine SSH/psql
  dependency.*
- **The one button is the dangerous one** — unconfirmed "Re-rank everything (90 days × active
  profiles)" can enqueue thousands of serial jobs (days of GPU) with no count preview, progress, or
  cancel. **Fix:** confirmation showing the computed job count; offer per-profile scope
  (`backfill_profile` already takes one profile_id).
- **No Ollama/LLM health anywhere.** `/healthz` pings only the DB; the admin page never contacts
  `OLLAMA_URL`. If Ollama dies, pending creeps up, jobs burn 5 attempts into dead-letter, embeddings
  decay — no alert fires. **Fix:** an Ollama up/down status card (one ping, model + embed model);
  optional deep-health `/healthz` variant.
- **Failing source is invisible** on the source-health table (only name / last_fetched_at / count;
  no error, no streak, no stale highlight) even though `last_error`/`consecutive_failures` sit on
  `app.state` and the route just never reads them. **Fix:** add last-error / streak / stale-highlight
  columns; route `LookupError` through the alerter; banner-warn when `ADMIN_ALERT_EMAIL` is unset.
- **Dead-letters are a bare counter.** `RankingJob.last_error` is persisted but rendered nowhere; no
  requeue. **Fix:** a dead-letter table (call title, profile, last_error) + per-pair retry. *Cuts
  post-Ollama-outage recovery from "wait 7 days for the suppress window" to one click.* `[ranking-surface]`
- **Queue panel can't tell "draining" from "stuck."** Only status counts, no oldest-pending age, no
  throughput, no auto-refresh. **Fix:** oldest-pending age + last-hour throughput + a 30s HTMX poll.
- **Scheduler is invisible and crash-fragile.** In-memory APScheduler jobstore; a restart/deploy over
  Monday 07:00 silently skips the whole week's digest (the 24h misfire grace only protects a
  paused-but-alive process). No view of registered jobs / next-run / last-run; `/healthz` stays 200
  if APScheduler is wedged. **Fix:** a `job_runs` table written by a tick wrapper + a "last 24h" panel;
  a startup catch-up check (if past Mon 07:00 and no `DigestRun` this week, run it); consider a
  persistent jobstore.
- **Alerting covers exactly one failure class** (ingestion streaks) and is off by default. Everything
  else (digest tick, golden eval, embedding refresh, reconcile, proposal-assist ticks) only logs to
  stdout.
- **Drafting kill-switch & token caps require SSH + env edit + restart** — the panel shows EXCEEDED in
  red but the operator can't act from it. Failure table renders raw workspace IDs + pretty-printed
  JSON. **Fix:** settings-table-backed toggle/override; human columns (workspace title, owner email).

---

## Theme 7 — Proposal-assist workspace seams

Impressive machinery behind broken connective tissue; only Phase 0 of the designed
Spec→Plan→Draft→Review pipeline is built.

- **The rubric-review gate is orphaned.** Phase 0 parks every workspace at
  `waiting_for_rubric_review` ([setup.py:373](../../../src/call_manager/proposal_assist/phases/setup.py#L373))
  but **no template links** to the page that unblocks it; creation redirects straight to chat. The
  owner has no discoverable path to advance their own workspace. **Fix:** when `current_phase=='setup'`,
  replace the composer with a status card linking to `/workspaces/{id}/rubric-review`; query
  `WorkspaceSetupJob` to show setting-up / failed states. *Highest leverage, small change.*
- **The ask→see-result loop is broken.** The SSE bridge streams only chat rows; nothing refreshes the
  proposal pane when a turn completes — "ask the agent to revise, see the revision" needs a manual
  full-page reload no one mentions. **Fix:** emit a turn-complete SSE event that `hx-get`s the artifact
  pane.
- **Failed turns are silent.** `_finalize_failed` writes the error only to `turn_jobs` + a
  `WorkspaceEvent`; no chat row, so a 600s timeout looks identical to "the assistant ignored me." The
  error text exists only on the admin panel — making the admin the human error-display for drafting.
  **Fix:** write a `role='system'` error chat row; add a Cancel button + "working…" indicator (the
  cancel endpoint already exists).
- **Error responses eat the typed message.** `post_turn` returns helpful cap/busy guidance as HTML
  4xx/5xx bodies htmx won't swap; the composer's `hx-on::after-request="this.reset()"` fires on
  failure too. **Fix:** return errors as JSON detail (the app.js handler shows those) or add the
  response-targets extension; gate the reset on 2xx.
- **First-message race:** the git repo is created only by the 30s Phase 0 worker, but the composer is
  live immediately; a message typed in the first seconds runs the agent against a nonexistent repo and
  fails silently.
- **Raw DB IDs in the UI:** new-workspace form asks for "Linked call ID" / "Profile ID" as bare number
  inputs; the workspaces list shows raw `call_id` / `owner_id` and a hardcoded grey status badge.
  **Fix:** resolved call title (read-only) + profile dropdown; join titles/emails; color the badge.
- **No DOCX/PDF export** — markdown only, explicitly deferred. Funders take Word/PDF, so the last mile
  of every proposal is a manual reformat that undercuts the citation-ledger work. **Fix:** pandoc DOCX
  export of the composed document. *Cheapest credibility win in the newest subsystem.*
- **Tool chatter clutters the transcript** (every tool call/result is its own chat row); **version
  history is read-only** (diff endpoint unlinked, no restore); **no link back to the call** from the
  workspace; **deadline captured but inert** (not in header, not prefilled, no reminder).
- **The phase chip is misleading** — renders raw `setup`/`spec` with no explanation while the agent's
  tool surface is `proposal.md`-only. **Fix:** ship a minimal phase UI or hide the chip until phases
  are real.

---

## Theme 8 — References & citations

The evidence ledger is strong but ringed with dead ends and an island posture.

- **PMID fetch always fails after a successful download.** `fetch_pubmed_pmid` returns mime
  `application/xml`, but `_MIME_TO_SUFFIX` has no XML entry → `ValueError` at ingest. PubMed is *the*
  database for hospital researchers and the advertised input that matters most always dies. **Fix:**
  add an `application/xml` (JATS) entry + a JATS text extractor + one end-to-end test. *One-line core
  + small extractor.*
- **Silent-ish upload/fetch errors** — error fragments aren't swapped by default and the forms reset
  regardless of outcome (note the Theme-0 accuracy caveat: a global handler *does* show a generic
  notice; the bug is generic text + lost input). **Fix:** stop resetting on failure; render the
  response fragment into a toast region.
- **No way to delete a reference** — `delete_or_tombstone_reference` is fully implemented and tested
  with **zero production callers**. A researcher who uploads a wrong or sensitive PDF can't remove it.
  **Fix:** wire a Delete button (route only; logic exists). Pair with a **Retry** button on failed
  rows (both specs promised it; bytes are retained for exactly this).
- **No metadata-only references** — a failed fetch inserts no row even when Crossref/PubMed returned
  complete metadata; every citation must bind to a stored chunk+quote, so you can't cite a paywalled
  paper you've read (most of a grant intro's citations). *Largest conceptual gap vs. how researchers
  actually cite.* **Fix:** metadata-only fallback on fetch failure; citable metadata-only entries.
- **No reference-manager interchange** — zero BibTeX/RIS/Zotero/EndNote support; researchers
  re-acquire and hand-correct every paper. **Fix:** `.bib`/`.ris` import (kills most of the
  metadata-review burden) + `.bib` export.
- **Approving metadata silently *downgrades* it** — the review form rebuilds CSL from only 5 fields,
  discarding volume/issue/page/publisher captured by extraction, and the bibliography prefers the
  rebuilt record. The diligent user gets a thinner entry than the unreviewed one. **Fix:** merge,
  don't replace; show all extracted fields; add "fetch metadata by DOI."
- **Blocking ingest with no progress** (extract + per-chunk embed + 60s LLM metadata, all synchronous)
  — 15–90s frozen Upload button per PDF. **Fix:** return the row in "pending" and poll (the annotation
  strip already establishes the pattern).
- **The Citations panel is a dead-end** — links promise "review in the evidence ledger" but the table
  shows no semantic verdict/rationale (both sit unused in `citation_audit_verdicts`), no per-row status,
  no deep-link to the cited section/reference; renders a raw UUID. **Fix:** make it the
  submission-readiness checklist a PI would actually run (verdict + rationale + deep links + re-accept /
  remove buttons).
- **`claim_drift` is uncleeareable by the human** — editing a cited sentence flags an error only the
  drafting agent's `update_citation(reaccept_claim=true)` tool can clear, so the owner must go to chat
  and ask the agent to bless their own typo fix.
- **No duplicate detection** (`original_sha256` computed, never checked); **references listed by
  sanitized filename, not title**; **no inline quote-in-context view**.

---

## Theme 9 — Cross-cutting concerns (no single subsystem owns these)

- **Swedish/English language axis runs through the whole ranker.** `[ranking-internal]` Vinnova bodies
  are Swedish; profiles are bootstrapped from PubMed into English; the embedding pre-gate **hard-fails**
  low-cosine pairs with no LLM review, and dev runs an English-centric embedder (mxbai, the bge-m3 NaN
  workaround). Cross-lingual cosine is systematically depressed → the Swedish calls are the ones most at
  risk of silent exclusion. Every planned next source (Cancerfonden, VR, Forte, ALF) is Swedish, so the
  Theme-4 coverage fix feeds straight into this. The rubric's verbatim-quote verification is also fragile
  on a 4B model reading Swedish. **Coordinate with the ranking work** — this is the single most important
  cross-cutting risk and no golden-set item, test, or spec addresses it.
- **GDPR / data-protection posture** is the real adoption gate for a second department's infosec review,
  and it's unexamined. Default email transport is **Resend (US processor)** carrying magic links + digest
  content; the reference uploader invites `.csv/.xlsx/.ipynb` datasets that flow to Anthropic via the
  agent's read/search tools (beyond "proposal text"); there is **no retention/deletion** anywhere (Article
  17); PubMed harvests researcher personal data into profiles with no processing-record framing. **Fix:**
  a data map, retention policy, deletion paths, an EU/on-prem email transport default; and the still-UI-only
  cloud-LLM disclosure gate needs **server-side** enforcement (the 2026-06-10 security spec is docs-only —
  see meta-finding).
- **Duplicate pursuit / the discovery↔drafting seam.** Discovery is communal (shared profiles surface the
  same call to every member) but workspaces are private and the call page always shows "Start drafting
  workspace" with no "a workspace already exists for this call" check. Two researchers can draft against the
  same call for weeks — duplicated effort, or two competing applications from one hospital to one funder.
  And there's no org-level pursuit view ("who is applying to what, due this month") despite the
  drafting/submitted/awarded status enum existing. **Fix:** existing-workspace check on the call page; a
  grants-office pursuit dashboard.
- **Onboarding the second department is a stop-the-world event.** No tenancy and no queue isolation: one
  global `/profiles` list, a single alphabetical dropdown, /me checkboxes for *all* profiles, a single
  global admin role. A department of ~10 groups means ~10 backfills into a priority-less queue — by the
  reviewers' (worst-case) math, potentially days of GPU during which department 1's rankings and digest
  degrade. **Fix (when growth forces it):** queue priority lanes `[ranking-internal]`; department grouping /
  ownership labels in the profile UI.
- **Mobile is where the Monday digest is actually read, and it's effectively read-only there.** Digest links
  open in mail-app webviews that never carry the 30-day cookie → every click hits the magic-link wall, and
  the requested link arrives back in the same webview. The whole app.css has one responsive breakpoint
  (1024px). **Fix:** the Theme-2 interstitial + tokenized per-link auth is the actual mobile-viability fix,
  not just deliverability polish; responsive pass on the feed.
- **No institutional memory of annual funding cycles.** Swedish funders repost essentially the same call
  yearly; each reappearance is a brand-new row keyed on `(source_id, external_id)` with no recurrence link,
  so "we won this last year" — the strongest relevance signal a hospital has — is invisible to ranking,
  digest, and UI. Workspace outcomes (submitted/awarded) feed nothing. **Fix:** recurrence linking; a
  grants-office annual-deadline calendar / `.ics`; outcome → relevance signal `[ranking-internal]`.

---

## Theme 10 — Ranking surfacing & feedback  `[ranking-surface]` / `[ranking-internal]`

UI/feedback items adjacent to the ranking work — **coordinate before acting.**

- **No feedback loop at all.** `[ranking-surface]` The founding spec's "Mark relevant / not relevant"
  was never built; the only signal is irreversible Hide, which teaches nothing. The cheapest source of
  golden triples (user clicks) is discarded. **Fix:** two buttons + a small table; on hide/good-match
  write a golden-set triple. *Near-zero UX cost; richest tuning signal.*
- **The quality alarm has never been armed.** `[ranking-internal]` `tests/fixtures/golden_set.json` is
  literally `[]`; the weekly drift eval + admin tau panel have measured nothing across a prompt march
  (→v4.1) and a Q8→Q4 quantization change. **Fix:** seed 30–50 hand-labeled triples + the admin
  curation flow (v2.1 spec P2). *Coordinate — this is squarely the ranking work.*
- **Unranked new calls sink below score-0 junk** (`NULLS LAST`) the morning after ingest — the most
  interesting rows are at the very bottom. **Fix:** sort pending-but-unranked above score-0, or split
  "Ranked / Awaiting ranking / Excluded by gates" sections. `[ranking-surface]`
- **No "ranking in progress" state on call detail / feed** (a queued/running/dead-lettered pair all look
  like "did nothing"); ranking visibility depends on a `?profile=` URL param with no on-page picker.
  (The first-run spec adds a status *strip*; this is the per-call/per-detail version.) `[ranking-surface]`
- **The explainability UI can lie** — hardcoded "pass threshold 0.30" while the real threshold is a
  setting and dev uses a different embedder; gate details recomputed from *today's* profile next to
  point-in-time scores; `ranked_at`/`prompt_version` stored but never rendered. **Fix:** render
  provenance + live threshold + a staleness chip. `[ranking-surface]`
- **"Top match" means three things** — ≥70 dashboard badge/summary vs. ≥60 digest bucket; Monday's
  celebrated match arrives as a mid-grey badge. **Fix:** define once in config. *Trivial.* `[ranking-surface]`
- **Profile edit fires an invisible multi-hour full backfill even when only a deterministic-gate field
  changed.** `[ranking-internal]` **Fix:** diff edited fields; re-gate budget/TRL changes in SQL without
  the GPU; reserve the LLM backfill for description/areas/exclusion changes.

---

## Meta-finding — spec/reality drift

The two newest "approved" specs are **docs-only commits**: the documented state has pulled ahead of the
running state in ways an operator could deploy on falsely.

- **User-system & security hardening (2026-06-10)** — `auth.py` still auto-provisions any allowed-domain
  address as an active user; no roles, no approval, no offboarding except psql; the cloud-LLM disclosure
  gate is still UI-only with no server-side enforcement (the spec itself rates this HIGH).
- **Ranking v2.1 (2026-06-11)** — design + plan only.

**Action:** either implement P1 of the 2026-06-10 spec before any internet exposure, or mark both specs
`Status: not yet implemented` so nobody deploys on false assumptions.

---

## Suggested sequencing (reviewers' view, not a commitment)

1. **First-run value loop** ✅ (spec'd) — makes the product work by default.
2. **Login & digest deep-link** (Theme 2) — the highest-leverage-per-line fixes; without them the
   re-engagement channel is a coin-flip, especially on mobile.
3. **Feed hygiene** (Theme 3, the expired-call slice) — restores trust in the score column; cheap.
4. **One Swedish medical-funder adapter + "sources we watch" + add-by-URL** (Theme 4) — attacks the
   coverage gap the founding spec says matters most.
5. **Admin levers + health** (Theme 6) — lets a non-developer actually run it.
6. **Proposal-assist seams** (Theme 7) — make the newest subsystem usable end-to-end.

Cross-cutting (Theme 9: language, GDPR) and ranking feedback (Theme 10) should be folded into whichever
adjacent work touches them, and the language/GDPR risks raised with the ranking and infosec owners
respectively.
