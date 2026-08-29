# Call-Monitor Architecture Review — Third Pass (Expansion)

**Date:** 2026-08-29
**Scope:** Expansion of [2026-08-29-second-pass-architecture-review.md](2026-08-29-second-pass-architecture-review.md)
**Status:** Review of record. Supersedes the second pass's severity ranking and its
central verdict.
**Code changes:** None; this is a review artifact.

---

## 0. What changed, in one paragraph

The second pass concluded **"there is no verified P0 defect in this review"** and
graded the architecture **B+**. That conclusion was true about the ground it
covered and false about the system, because it inherited its scope from the review
it was auditing. This pass opened seven subsystems neither earlier review looked
at. It found a **measured, reproducible, authenticated-low-privilege
whole-instance denial of service**: one legal 1,000,000-byte WebSocket frame from
any workspace member — including a read-only viewer — freezes the entire
single-instance process for **105.87 seconds**, and can be resent indefinitely.
That is a P0 by any definition. The rest of the second pass's findings survive
largely intact, with corrections; the expansion adds **1 critical + 14 high + 26
medium/low** confirmed findings, and identifies **three systemic patterns** that
explain why these defects recur.

---

## 1. Method and evidence standard

92 agents across six phases: re-verify every second-pass claim → adversarially
refute each verification → triage the 60-item unverified backlog → sweep 14
subsystem lenses the review never opened → adversarially judge every new finding →
three completeness critics. 13 proposed findings were killed by their judges and
are listed in §8.

The evidence rules imposed on every agent: no number that was not produced by an
executed command; a real `file:line` is not proof of a guarantee (hunt the early
return, the decorator, the caller-side guard); style or size alone is not a defect
without a concrete harm scenario; a test "covering" something must be checked for
whether its assertion can actually fail.

**Where two agents disagreed, this document reports a measurement made directly,
not a vote.** Two such disputes arose and both are resolved below (§2, §4.6).

### Measurements made directly for this document

| Check | Command | Result |
|---|---|---|
| Adversarial varuint at the exact frame cap | `handle_sync_message(b"\x00" + b"\xff"*999_998, Doc())` | **105.87 s** blocking CPU, then `IndexError` |
| Same, scaling | 32 KB / 64 KB / 128 KB / 256 KB | 0.110 s / 0.425 s / 1.683 s / 7.038 s — clean quadratic |
| Raw Python throughput, this box | `timeit` | 57.4 M simple statements/s; 26.6 M function calls/s |
| Source size | `find src -name '*.py' \| xargs cat \| wc -l` | 54,571 lines (42,132 non-blank/comment) |
| Whole-app import | `python -c "import call_manager.main"` | 3.35 s (once, at boot) |

---

## 2. The headline reversal: there is a P0

### P0-1 — Unbounded varuint in the CRDT sync decoder freezes the whole instance

**Severity:** Critical · **Confidence:** Measured end-to-end · **Status:** Confirmed by direct reproduction

`Room.handle` dispatches on `P.is_sync_step1(message)` at
[room.py:155](../../../src/call_manager/proposal_assist/collab/room.py#L155) — **before**
the `state.can_write` gate at line 176. The branch's own comment says so:
*"reachable by any connected member (the read path is ungated)."* It then decodes
the frame at [room.py:164](../../../src/call_manager/proposal_assist/collab/room.py#L164)
**inside `async with self._lock`, on the event loop**:

```python
async with self._lock:
    try:
        reply = P.handle_sync_message(message[1:], self.doc)
    except Exception:
        logger.warning("collab.malformed_step1", ...)
        return
```

`handle_sync_message` → `pycrdt.read_message` → `Decoder.read_var_uint`, an
unbounded `while True` accumulating `uint |= (byte & 127) << shift`. On a run of
continuation bytes this shifts a multi-megabit Python int in an O(n²) loop.

The WebSocket receive loop caps frames at
[collab_ws.py:58](../../../src/call_manager/proposal_assist/web/collab_ws.py#L58)
`_MAX_FRAME_BYTES = 1_000_000`, rejected at line 223 with `len(message) > _MAX_FRAME_BYTES`.
A frame of **exactly** 1,000,000 bytes passes.

**Reproduced for this document:**

```
frame is exactly 1000000 bytes -> passes the cap check at collab_ws.py:223
is_sync_step1 = True   (so room.py:155 takes the READ branch, ungated by can_write)

  ONE legal 1,000,000-byte frame -> IndexError after 105.87 SECONDS of blocking CPU
```

Scaling confirms the mechanism is quadratic, not a one-off:

| payload | time |
|---|---|
| 32 KB | 0.110 s |
| 64 KB | 0.425 s |
| 128 KB | 1.683 s |
| 256 KB | 7.038 s |
| 999,998 B | **105.87 s** |

**Harm.** The project runs a single asyncio loop under a single-instance hard
constraint (CLAUDE.md; `room.py:9-12`). For those ~106 seconds *nothing in the
installation advances*: every HTTP request, every SPA call, every other
workspace's collab socket, every SSE stream, every `asyncio` deadline in the
retirement teardown, and every `run_coroutine_threadsafe` future dispatched from
scheduler threads. The room's `_lock` is held throughout, so that workspace's
writers stall too. Authorization to reach it is **membership only**
(`collab_ws.py:100-107`); `can_write` is irrelevant. The frame can be resent in a
loop. There is no failover. The `try/except` catches the `IndexError` only *after*
the CPU is spent.

**Coverage: hollow, and the correct assertion exists 60 lines away.**
[test_collab_room.py:222-239](../../../tests/proposal_assist/collab/test_collab_room.py#L222)
is the only malformed-STEP1 test; it uses a five-byte frame
`bytes([0, 2, 0xFF, 0xFF, 0xFF])` and asserts only "no persist, no broadcast" — an
assertion true before and after any bound. Meanwhile
[test_collab_protocol.py:180-190](../../../tests/proposal_assist/collab/test_collab_protocol.py#L180)
asserts `time.perf_counter() - t0 < 2.0` against `b"\xff" * 900_000` — exactly the
right gate, applied to the *awareness* decoder only.

**This is a regression of a fix that was already made.** Commit `f535f6c` hardened
the varuint DoS by hand-rolling a bounded reader `_rvu` at
[protocol.py:120-133](../../../src/call_manager/proposal_assist/collab/protocol.py#L120),
inside **one function**, `read_awareness_entries`. Its docstring names the
mechanism verbatim — *"an O(n^2) CPU stall that blocks the entire single-instance
asyncio event loop — a whole-process DoS, since the awareness path is ungated by
`can_write`"* — and every clause applies to STEP1 unchanged. But `extract_update`,
`extract_awareness_update` and the re-exported `handle_sync_message` all still call
the unbounded `pycrdt.read_message`, which remains importable and remains in
`protocol.__all__`. **The fix was written as a function, not as the only door.**

**Action.**
1. Add `safe_read_message(...)` in `protocol.py` wrapping the existing `_rvu`
   bounded reader; route STEP1, SYNC_UPDATE and `extract_update` through it.
2. Remove `read_message` from `protocol.__all__` and from the import surface, so
   the next contributor cannot re-open the door.
3. Clone the `< 2.0 s` timing assertion from `test_collab_protocol.py:180` onto
   STEP1 and SYNC_UPDATE frames at the cap.
4. Consider moving the decode off the loop (`run_in_threadpool`) as defence in
   depth — it does not fix the CPU burn but it stops it freezing unrelated work.

**Note on a contradicting report.** One verification agent measured 0.005 s and
declared this refuted. It built a *well-formed* 999,993-byte state vector
(short varuints, fast) and separately passed raw `b"\xff"*1MB` to a different
entry point. Neither is the adversarial input. The measurement in the table above
is the exact call `room.py:164` makes, and it reproduces on demand.

---

## 3. Disposition of the second pass's own findings

Every claim was re-derived from code. All eleven survive as mechanisms; the
severities move, and the citations need correcting.

| Claim | Verdict | Severity after judging | Note |
|---|---|---|---|
| P1-1 Coherence pre-launch race | **Confirmed** (proved by a scratch test that made Coherence launch after cancel) | medium → **low** | Review says "Evaluator and Uncited Claims"; **Review/pass_runner.py:96 has the guard too**. Three of four families have it; Coherence is the sole outlier. A racing mitigation the review omits: `PASS_REGISTRY.retire_workspace` cancels the pass task (`passes.py:134`), so this is a lost race, not an unguarded path. |
| P1-2 Coherence/Evaluator lose late spend | **Confirmed** | **medium** | Blast radius understated: the dropped columns feed the **org-wide** `check_global_daily_token_cap` too, so one family's drop raises the ceiling for chat turns in *unrelated workspaces*. Evaluator has a **second, deterministic (non-race)** drop at `pass_runner.py:177-180`. |
| P1-3 Disclosure detected by regex | **Confirmed** | medium → **low** | The gate itself never fails open — `_cloud_gate.py:40` still raises 403 and blocks the hosted call. The harm is UX, not disclosure. But it is wider than stated: `gate_cloud_llm` has **five** call sites, and the four panel endpoints have no ack path at all. |
| P1-4 API tests leak engines | **Confirmed leak, harm refuted** | medium → **low** | Scale understated ~36×: **36 test files** build an undisposed `create_app()`, not one. But the harm chain is defeated: `PGconn.__del__` → `PQfinish` does close the socket at GC, and production has exactly one `create_app` call site. |
| P2-1 Collab orchestration duplicated 4× | **Partial** | low → **none** | Four guards make the harm unreachable (mutex-first ordering in all four bodies). Off by one: **three** clean-error tuples for four entry points, not four. The real finding here is a coverage hole, not duplication (see §5). |
| P2-2 Fence conversion active | **Confirmed** | low → **none** | Anchor off by five (`SLICE_STATUS` is line 80, not 75). `SLICE_STATUS["F3f"] = "pending"` tracks **zero** inventory rows, so the review's own action "finish F3f first" is currently **unfalsifiable**. |
| P2-3 Tool registry oversized | **Confirmed counts** | low → **none** | Counts hold. Maintainability debt, no harm story. |
| P2-4 No frontend lint gate | **Confirmed** | medium → **low** | Confirmed; process risk. |
| CORR-A / CORR-B corrections table | **Partial / Confirmed** | — | The dispositions hold. |
| Headline verdicts + evidence table | **Confirmed as stated** | — | The numbers reproduce. **But see §0 — the "no P0" conclusion is refuted by evidence outside its scope.** |

### Citation corrections (for anyone patching by line number)

- `coherence/pass_runner.py#L67` — **wrong anchor.** Line 67 *is* a status check
  that exists. The gap is lines 87–106; cite **106**.
- `evaluator/pass_runner.py#L120`, `uncited_claims/pass_runner.py#L141` — comment
  lines. Code is at 125–128 and 143–146.
- "token assignment at 170 / 183" — those are the *guards*; assignments are
  171–174 and 184–187.
- `client.ts#L8` — line 8 is the class declaration; `code: string` is line **9**.
- `_cloud_gate.py#L39` — line 39 is the guard; the `raise` is **40–42**.
- `tests/test_api_me.py:6` — line 6 is **blank**. `_app()` is line 7; the
  offending `TestClient` sites are 24, 30, 59.
- `tests/conftest.py:40` — that is the decorator; the disposal is line **53**, and
  there is a *second* disposal at line 95 the review does not mention.
- `collab_cite.py#L33` — a comment line. The three functions are at 56, 107, 158.
- `tests/fencing/inventory.py#L75` — blank. `SLICE_STATUS` spans **80–120**.
- `turns.py:122` contains a stale in-code comment citing `main.py:361`; the
  handler is at **main.py:500-508**.

---

## 4. New findings — critical and high

Fourteen high-severity findings the earlier reviews never reached. All survived an
adversarial judge instructed to default to "refuted".

### 4.1 SSRF refusal is fail-open (ssrf · high)

`pinned_transport_for()` returns `None` as its **refusal** signal — unsafe scheme,
blocked/private IP in any DNS answer, or DNS failure. Both adapters assign that
`None` straight into `httpx.Client(transport=...)`, where `None` means *"build the
default transport"*. The exact case the SSRF check rejected is the case that
connects with no pinning at all.
[eu_portal.py:176-185](../../../src/call_manager/sources/eu_portal.py#L176), byte-identical at
[vinnova.py:140-149](../../../src/call_manager/sources/vinnova.py#L140).

Poisoned DNS for the configured host answering `169.254.169.254` → the adapter
POSTs to the metadata endpoint and persists the JSON reply as funding calls. The
same fallback silently strips pinning on any transient `gaierror`, reopening the
DNS-rebinding window the pin exists to close.

*Coverage:* tests always inject `transport=`, so the production branch is never
exercised — the inline comment at `eu_portal.py:180` says so explicitly.

*Judge:* mechanism confirmed by execution; rated the practical harm lower because
it requires DNS compromise. **Fix is one line each**: `if t is None: raise AdapterError(...)`.

### 4.2 One non-string scalar permanently halts ingestion (ssrf · high)

[upsert.py:71](../../../src/call_manager/ingestion/upsert.py#L71) calls `len(currency)`
on a value the EU adapter passes through from upstream JSON with no `str()` and no
type check. `"currency": 0` raises `TypeError` before any DB work; it escapes to
the orchestrator's broad `except Exception` at `orchestrator.py:211`, the batch
transaction rolls back, and `last_fetched_at` is **not** advanced. The next tick
re-fetches from the same `since`, hits the same item, fails identically — forever.
Every other call in the batch is discarded on every tick.

A silent, permanent ingestion outage for that source, triggered by one upstream
field-type change — precisely the poison-record failure the guard's own comment
claims to prevent. `programme` has the mirror problem (a dict of len ≤ 200 passes
the guard and hits a `VARCHAR(200)`); `keywords` is built with a bare `list(...)`.

*Reproduced by the judge:* `upsert_call(None, ..., currency=5)` → `TypeError: object of type 'int' has no len()`, with `session=None` — no DB needed.

### 4.3 Ranking prompt fence can be closed by the call text itself (promptdata · high)

A funding call whose `summary`/`full_text` contains the literal line `CALL_BODY>>>`
closes the fence early. Everything after it renders **outside** the region the
system prompt's SECURITY note designates as untrusted data.
[prompt.py:216-234](../../../src/call_manager/ranking/prompt.py#L216) interpolates with
only a length truncation — no escaping.

Reproduced independently by both the finder and the judge: the injected sentence
lands after the forged marker. Harm chain: the call self-scores `topic_fit 5 /
eligibility pass` → clears `RANKING_ESCALATION_MIN_SCORE` → is re-ranked by hosted
Sonnet with `enable_analysis=True` → its attacker-authored text is persisted and
**mailed to analysts in the weekly digest**.

*Coverage: hollow.* `test_ranking_prompt.py:96-103` and `:139-145` partition on the
markers, but every fixture body is marker-free, so both pass unchanged against a
forged delimiter. No test body in `tests/` contains `CALL_BODY>>>`.

### 4.4 19 async MCP tool handlers do blocking work on the app event loop (concurrency · high)

The in-process SDK MCP server awaits every tool handler **on the caller's loop**.
Verified: `main.py:282` hands the lifespan loop to `build_scheduler` as `app_loop`;
`scheduler.py:762` dispatches the turn drain via `run_coroutine_threadsafe(coro, app_loop)`;
and a grep for `to_thread`/`run_in_executor` across the entire installed
`claude_agent_sdk` returns exactly one hit, in CLI path discovery — none around
tool dispatch.

Worst case: `search_references` at
[references.py:124](../../../src/call_manager/proposal_assist/tools/references.py#L124)
does `embedder.embed(query)` — a **synchronous** `httpx.Client` POST to Ollama with
default `timeout=60.0`. Ollama serves ranking, embedding and all four pass runners
from one on-prem 2080 Ti; a queued GPU makes this seconds to a minute of frozen
loop. Separately, four handlers call `ports.effects.*`, each opening its own session
and doing a blocking `acquire_shared_lease` bounded at 10 s — with a human save
holding the exclusive commit mutex, each freezes the loop for up to ten seconds.

*Caveat recorded by a critic:* the "19 of 24" count has an undisclosed selection
rule and the anchor is an `*_impl`, not a handler. **Re-derive the count before
citing it.** The mechanism is not in doubt.

### 4.5 `controller.launch` blocks the loop on a lease wait and an unbounded `git log` (concurrency · high)

`async def launch` at
[controller.py:81](../../../src/call_manager/proposal_assist/drafting/controller.py#L81)
contains **zero** threadpool hops (verified: `grep -c 'run_in_threadpool|to_thread|run_in_executor'`
→ `0`) and calls, synchronously, `reconcile_workspace_git_drift` (:167) and
`reconcile_workspace_citekeys` (:175).

Both reconcilers call `acquire_shared_lease` ([reconcile.py:84](../../../src/call_manager/proposal_assist/reconcile.py#L84), :246),
which is `pg_advisory_xact_lock_shared` under `SET LOCAL lock_timeout='10000ms'`
([fencing.py:214-217](../../../src/call_manager/proposal_assist/fencing.py#L214)). Shared
conflicts with a concurrent section save's exclusive hold, so this SQL blocks up to
**10 seconds inside a coroutine that never yields**. `reconcile.py:56` runs
`git log --pretty=...` with no `-n` and no `timeout=`. The resulting
`CommitMutexTimeout` is swallowed by `except Exception` at `controller.py:185`, so
the freeze is invisible in logs as anything but latency.

**Correction to the original finding:** it attributed the 10 s wait to
`acquire_workspace_lock`. That is wrong — `locking.py:75` is
`pg_try_advisory_lock`, explicitly **non-blocking**. The 10 s is in the shared
lease inside the reconcilers. *Verified directly for this document; the finding
stands with the attribution corrected.*

### 4.6 Late/failed drafting spend is lost on the SDK-error path (llmops · high → medium)

**This finding was disputed and is narrowed.** The review draft claimed *every*
failed turn records zero tokens. That is wrong: `record_terminal_outcome`
([outcomes.py:81](../../../src/call_manager/proposal_assist/turns/outcomes.py#L81))
computes `consumed = tokens_in > 0 or tokens_out > 0` and, when consumed, stamps
the tokens onto a system `ChatTurn` row — and `check_daily_token_cap`
([caps.py:114-119](../../../src/call_manager/proposal_assist/turns/caps.py#L114)) sums
`ChatTurn` with **no role filter**, so that row counts. The B9 fix works.

The real, narrower defect: `claude_adapter.py:462` raises `SDKTurnError`
**before** the token-capture block that follows it, so on an SDK-reported API
error (529 overloaded, etc.) the running `am_tokens_in` tally is discarded and
`tokens_in=0` reaches `outcomes` → `consumed=False` → the submit-time pre-count is
*cleared*. Same on cancellation. Net: a turn that streamed real Opus output before
erroring records **zero** against the cap. Nothing rate-limits resubmission, and
`grep -rn max_turns src/call_manager/` returns **zero hits**, so a retry loop
during an overloaded-API window burns unbounded real money while
`DAILY_TOKEN_CAP_INPUT` stays at 0 used.

*Coverage: anti-covered.* `test_turns_processor.py:626` **pins** the clearing
behaviour, and its own fixture contradicts its docstring — the stub yields
assistant text and *then* raises, i.e. the model demonstrably produced output,
under the docstring *"A turn that fails never produced a real response."*

### 4.7 Every deliberate 5xx message is replaced with "internal server error" (errors · high → medium)

[main.py:500-508](../../../src/call_manager/main.py#L500) — the whole handler, no early
return, nothing shadows it — discards `exc.detail` for any `/api/*` path when
`status_code >= 500`.

An admin sets `drafting_enabled=false`. `gate_cloud_llm` raises
`HTTPException(503, "Drafting is currently disabled by an administrator.")`. The
user is shown **"internal_error: internal server error"**, and support has nothing
to distinguish an intentional switch from a crash. Same flattening hits
`admin_retention.py:465-471` (whose own comment says *"the admin must be told"*),
`collab_checkpoint.py:91` (mutex contention → looks like a crash, no `Retry-After`,
while the identical condition elsewhere is a readable 429), and `references.py:236`.
Secondary: `STATUS_CODES` has no 502/503 entry, so those statuses cannot be
documented at all.

*Coverage: hollow.* `ReviewPanel.test.tsx:114-131` fabricates the error
client-side and asserts on a message **production can never emit** — and its inline
comment repeats the false claim that the backend detail surfaces.

### 4.8 `synced` latches on a frame that delivered nothing (crdt · high → medium)

[collabProvider.ts:205](../../../frontend/src/collab/collabProvider.ts#L205) treats
`SYNC_UPDATE` as proof the document arrived. The server registers a joining socket
in `room.clients` at `collab_ws.py:144` but only sends the initial STEP1 at
`collab_ws.py:218` — with two `run_in_threadpool` DB round-trips in between. Any
peer editing in that window has its update broadcast to the new socket *first*;
its dependencies are missing, Yjs buffers it as pending, the doc stays **empty** —
and `_synced` latches true anyway. `CollabEditor.tsx:57` gates the whole editor on
`synced`, so Tiptap mounts a blank, editable draft for a document that has content.

The file's own comment records this happening in production: *"the room held 4,347
bytes and the Outline listed seven sections while the editor showed nothing at
all."* If the user types in that window, their text merges at the wrong place once
STEP2 lands.

*Coverage: none.* `collabProvider.test.ts:333-389` is literally named *"synced
latches on CONTENT, not on any sync frame"* and tests only STEP1 and STEP2 — not
the SYNC_UPDATE case that reintroduces the bug the describe block is named for.

### 4.9 Collab stops reconnecting after 12 tries while the UI says "reconnecting…" (frontend · high)

`maxAttempts = 12`; the backoff sums to **exactly 241,000 ms** (computed, not
predicted). After that,
[collabProvider.ts:243](../../../frontend/src/collab/collabProvider.ts#L243) returns
with no `setStatus` and no listener call, and `connect()` is re-entered from only
two places — the constructor and the reconnect timer. Nothing ever reconnects for
the life of the mount. `attempts` resets only in `ws.onopen`, which by definition
cannot fire again.

Meanwhile `synced` is latched, so the editor stays mounted; `useEditor` was
configured `editable: props.canWrite` and is never re-configured, so the surface
stays typeable; and the only feedback is StatusBar's label, literally
**"Offline — reconnecting…"** — a statement that is false from second 241 onward.
The user types for an hour into a Y.Doc with no socket and no client-side
persistence (no `y-indexeddb` in the tree); `send()` silently no-ops because
`this.ws` is null; every character is destroyed by the next reload. "Save a
version" is also disabled, with no explanation.

*Coverage: hollow.* The only gate asserts `sockets).toHaveLength(3)` — that
reconnects stop — and makes no assertion about what the user is told.

### 4.10 "Save a version" never invalidates the Versions list (frontend · high → medium)

[useSaveVersion.ts:15](../../../frontend/src/collab/useSaveVersion.ts#L15) invalidates
`[artifact, id]` — i.e. `["draft", id]`. The version list lives under
`["versions", id, artifact]`, and **nothing in the entire frontend ever invalidates
that key** (the literal appears as a query key exactly once and in no
`invalidateQueries` call, across 70+ call sites). With `refetchOnWindowFocus:
false`, `staleTime: 20_000` and no `refetchInterval`, the pane keeps rendering the
pre-checkpoint list. The version the user just created is absent and cannot be
selected for comparison. Clicking Save again is a server-side no-op that reports
nothing. The same gap applies to section save, proposal accept, and annotation
insert.

*Coverage: zero, not partial.* The only file touching the hook replaces it
wholesale with `vi.mock`; a mock with no QueryClient cannot observe an
invalidation.

### 4.11 SMTP-only deploy silently loses the weekly digest (config · high → medium)

Two disagreeing transport selectors over one env var. `create_app` picks
`SMTPEmailSender` on `smtp_host` alone ([main.py:409-416](../../../src/call_manager/main.py#L409)),
so ingestion alerts send. But the lifespan's digest client goes through
`build_email_client`, which reads `email_transport` (default `"resend"`) and raises
`EmailConfigError`. [main.py:171-177](../../../src/call_manager/main.py#L171) swallows
that into a single WARNING, leaves `digest_email_client=None`, and
`scheduler.py:468` then **never registers the `digest:weekly` job at all**.

An operator who follows the README and sets `SMTP_HOST` without `EMAIL_TRANSPORT`
(absent from `.env.example`, so never copied) gets: the product's core deliverable
never scheduled and never sent, permanently, on a fully green `/healthz`, with the
admin "send digest now" button returning `triggered=false`.

*Coverage:* nothing constructs `create_app` with `SMTP_HOST` set and
`EMAIL_TRANSPORT` unset; grep for `digest_skipped` returns zero hits.

### 4.12–4.14 Three hollow gates over high-stakes invariants (testquality · high)

**(a) Two "non-member" authorization tests use an unauthenticated client.** Delete
`require_member` from `get_scorecard` (evaluator.py:103) or from
`latest_uncited_claims_run` (uncited_claims.py:142), leaving the
`Depends(get_current_user)` that is already there. Both routers' only
membership-named tests **still pass**, because `client_unauth` has no session
cookie and is rejected before the route body runs. In production, any
authenticated hospital user could then read another research group's evaluator
scorecard and their verbatim extracted draft claims. Cross-tenant disclosure of
unpublished grant applications.

**(b) The cloud-disclosure gate test stubs the session, so per-actor ack scoping
never runs.** `test_cloud_gate.py` passes a stub whose `scalar()` returns `None`
unconditionally, so the `select(...)` built at `_cloud_gate.py:30-38` is discarded
and the `WorkspaceEvent.actor_id == user.id` predicate at line 35 is never
evaluated. The four cloud triggers are deliberately gated on `require_member`, not
`require_owner` — so per-actor acknowledgement is the *only* thing between a
non-owner and hosted-model disclosure. Delete the actor predicate and every test
stays green while viewer B's action ships the draft to Anthropic on the owner's
consent record.

**(c) The day-boundary helper no test can falsify.** Mutating
`_stockholm_day_start_utc` (caps.py:95) from Stockholm-local to UTC midnight leaves
the suite green (measured: `9 passed`, `3 passed`). `grep` over `tests/` for the
helper returns **zero** hits; no time-freezing library is installed anywhere. In
production this makes the 00:00–02:00 Stockholm window invisible to every cap for
the rest of the day — up to **2× the configured hosted spend**.

> **Correction to (c), from a critic:** there are **two** definitions, not one —
> `turns/caps.py:95` and a byte-different copy at `citations/usage.py:29`, plus a
> third week-granular implementation in `digest/send.py:45-58`. The citation-audit
> cap rides on the unshared copy, so a fix to `caps.py` would not touch it. The
> correct framing is *"three implementations, one of them the only guard on an
> uncapped cloud path."*

---

## 5. New findings — medium and low

Twenty-six further confirmed findings. Grouped; each has file:line, a harm
scenario, and a coverage verdict in the run record.

### Authorization / authentication

| Finding | Location |
|---|---|
| SSE turn stream authorizes once at connect; keeps streaming after logout, admin-disable, or member removal (every one of those bumps `session_epoch`) | `web/api/turns.py:394,406` |
| `add_member` matches collaborator email case-sensitively; `Anna.Svensson@ki.se` silently fails while login/register normalize | `web/api/settings.py:160` |
| Activity feed discloses collaborator email addresses to non-owner members, defeating the owner-only PII policy — its named test asserts the wrong thing | `proposal_assist/web/activity.py:82` |
| Login rate-limit slot consumed on **successful** logins: 5 sign-ins in 15 min locks the account out | `web/api/auth.py:90` |
| `POST /api/auth/logout` accepts an epoch-stale cookie, so a revoked cookie remains a working force-logout button | `web/api/auth.py:205` |

### Cost, caps, and the hosted models

| Finding | Location |
|---|---|
| Global daily token cap enforced **only** on chat submits; the four member-triggerable cloud fan-outs never call it (its test asserts the counter, never the enforcement) | `proposal_assist/web/coherence.py:59` + 3 siblings |
| Compaction — the only SDK-session reset — serialises every un-summarized turn including whole tool_result payloads into one unbounded Opus call, with no cap check | `turns/compaction.py:152` |
| Drafting subprocess secret-scrub list omits `INITIAL_ADMIN_PASSWORD` and `GOOGLE_CLIENT_SECRET`, and pins `VINNOVA_API_KEY` which is not a Settings alias. Test is a subset assertion over four keys | `drafting/claude_adapter.py:87` |

### Git and the workspace repos

| Finding | Location |
|---|---|
| The review pass runner commits with **no commit mutex**, and bare `git commit` sweeps the whole index — it can commit another writer's staged files | `review/pass_runner.py:349` |
| Three agent-invoked git subprocesses run with no `timeout=` on the event loop | `tools/history.py:46,81,106` |
| `gitio`'s git env is not hermetic, unlike `phases/setup`'s — operator gitconfig can break every workspace write | `citations/gitio.py:39` |
| Private `_current_head` duplicate runs unbounded `git rev-parse` **while holding the commit mutex** | `citations/core.py:134` |

### Data layer

| Finding | Location |
|---|---|
| Three live indexes (incl. the **HNSW vector index**) absent from the ORM models, so `alembic --autogenerate` emits `DROP INDEX` for them. No `alembic check` in CI, and **no drift gate exists at all** | `db/models.py:1760` |
| `claim_classifications.reference_id` / `user_claim_annotations.reference_id` repeat the unindexed-FK defect migration 0033 was written to fix | `db/models.py:1924` |

> A critic could name only **one** of the three orphan indexes from the finding as
> written. Re-run `alembic revision --autogenerate` against a migrated container
> and name all three before acting.

### Errors and contracts

| Finding | Location |
|---|---|
| Claim-analysis job failures return raw `str(exc)` — SQL text and internal URLs — to any workspace member | `analysis/pipeline.py:650` |
| 19 of 109 `/api` operations sit outside the router that declares the error envelope; their generated 422 type is a lie *(this one was independently re-derived by walking `APIRoute`s — trust this count)* | `main.py:797` |
| Problems panel converts backend signal failures into a clean bill of health, with no field able to say "a signal failed" | `problems/aggregate.py:120` |
| Broad `except ValueError` reports internal failures as client 400s | `web/api/references.py:238` |

### Scheduler and ops

| Finding | Location |
|---|---|
| `trigger_job_now` reports success on a never-started scheduler, so admin "run now" silently no-ops — contradicting `main.py:229-230`'s explicit claim that it still works | `scheduler.py:1129` |
| Lost scheduler lock silently kills all background work; no retry, no heartbeat, and `/healthz` cannot express it | `main.py:233`, `scheduler.py:54` |
| `/healthz` asserts DB connectivity only — a restart that skipped `alembic upgrade head` reports healthy | `web/routes/health.py:19` |
| `WORKSPACES_DIR` default is CWD-relative with no boot-time validation of the confidential data root | `config.py:178` |

### Collaboration and editor

| Finding | Location |
|---|---|
| A WebSocket **TEXT** frame raises an unhandled `KeyError` out of the collab route | `web/collab_ws.py:222` |
| Post-save optimistic write reverts the server's newline canonicalization | `editor/ArtifactSectionEditor.tsx:173` |

### Coverage holes promoted from §3

- `init_spec_via_kernel` has **zero test references** — both its clean-error arm
  and its evict arm are wholly untested, while its docstring declares the
  invariant fragile (*"Do not reorder kernel_init_spec so that stops being true"*).
- The fifth `build_ports` site is unguarded, **proved by mutation**: setting
  `drafting/factory.py:100` to `ports=None` leaves `test_f3d1_spine.py` green
  (20 passed). The AST gate tests key *presence*, not value.
- No gate detects a stale `SLICE_STATUS` key: the check runs rows → keys only, so
  a key can outlive its rows silently — which is how `F3f` came to track zero rows.

---

## 6. The systemic patterns

The second pass framed the risk as *"localized behavioral drift at repeated
edges."* Three patterns recur too consistently for that framing to hold. Each has
the same shape: **a rule that is understood, documented, and correctly applied at
one site, with nothing structurally preventing the next site from breaking it.**

### 6.1 A fix written as a function, not as the only door

The varuint DoS was found and fixed once (`f535f6c`), with a docstring naming the
exact mechanism and the exact reason it is a whole-process DoS. The fix hardened
one function. The unbounded primitive stayed importable, stayed in `__all__`, and
is still called from three other entry points — one of which is now **P0-1**.

**Ratchet:** when a fix hardens a primitive, delete the unhardened primitive from
the import surface. A fix that leaves the sharp edge reachable is a fix with a
half-life.

### 6.2 Token spend has no ledger, only re-derivation

`turns/caps.py:192-228` computes today's usage by summing **five tables on five
different timestamp columns**. Every hosted-LLM feature must independently
remember to add two columns, write them on *every* terminal path, and get a
`SELECT` added to this function.

The second pass found two misses and graded them P1. This pass found **four more
of the identical rule**: evaluator's deterministic no-scorecard return before the
token write; coherence marking `completed` with tokens unset; compaction spending
uncapped and unaccounted; and four fan-out endpoints that never call
`check_global_daily_token_cap` at all. Plus a **second day-boundary
implementation** guarding the citation-audit budget alone.

Six independent violations of one invariant is not six defects. It is a missing
write-path.

**Ratchet:** an append-only `llm_spend(kind, ref_id, tokens_in, tokens_out, model,
sdk_session_id, at)` written once from the adapter's `TurnDoneEvent` handler; caps
read one table. Then an AST gate failing any module that imports an SDK adapter and
assigns `tokens_in`/`input_tokens` directly.

### 6.3 Async correctness is enforced at one layer and absent at another

An AST scan finds **28** `async def` functions in `src/call_manager` that construct
a synchronous Session outside a nested sync function: all four pass runners, eight
MCP tool impls, `turns/processor.py:177`, `drafting/controller.py:81`, and
`web/api/turns.py:314` — which issues two synchronous Postgres queries **every
200 ms per open SSE stream**, on the loop.

The route layer gets it right three times in one file (`collab_ws.py:104,127,140`).
So the convention exists, is understood, and is applied at one layer — with nothing
checking the other. Ruff's `ASYNC` ruleset is not even enabled
(`pyproject.toml:236`: `select = ["E","F","I","B","UP","SIM"]`).

**Ratchet (two steps, cheap first):** enable `ASYNC` today — it costs 4 fixes
(`ASYNC221` at `tools/history.py:46,81,106`; `ASYNC110` at `room.py:770`) and is a
free floor. Then write the real gate as an AST check with a pinned exemption list,
in the style the fencing work already uses.

---

## 7. The carried backlog — triaged, with a caveat

The second pass's §7 register held ~60 items explicitly marked *"not confirmed."*
Twelve agents triaged all of them:

| Disposition | Count |
|---|---|
| **PROMOTE** (a harm scenario exists) | 23 |
| **STYLE_ONLY** (real duplication/size, no harm) | 36 |
| **ALREADY_FIXED** | 1 |
| **STALE** | 0 |

**Highest-rated promotions:** *dual session acquisition patterns* and *shell
knowledge of cockpit details* (both rated high); *growing workspace hot row*,
*inconsistent reaper contracts*, *inconsistent transport policy across adapters*,
*mixed transport/retry/SSRF ownership in `sources/types.py`*, *private
httpx/httpcore IP-pinning internals*, *duplicated profile validation limits*,
*duplicated score-band thresholds*, *stringly TanStack query keys*, and *validator
imports private ranking helpers* (medium).

> ### Honesty caveat — read before acting on §7
>
> A completeness critic checked the triage output and found that **the rows carry
> no `file:line`, no executed command, and no harm sentence.** Under this
> project's own rule — style or duplication alone is not a defect — a PROMOTE
> without inputs→wrong-output is exactly the shape that gets rejected on review.
> The single `ALREADY_FIXED` asserts a fix with no commit sha.
>
> **Treat all 23 promotions as leads, not findings.** Before any of them enters a
> plan, it needs one `file:line` and one concrete failure sentence, or it falls
> back to backlog. This is the largest measurement debt in the pass, and it is
> recorded here rather than hidden.
>
> The 36 STYLE_ONLY dispositions are the more useful half of this result: they
> retire two thirds of a backlog that has been carried, unexamined, across three
> reviews.

---

## 8. Refuted — do not re-open without new evidence

Thirteen proposed findings were killed by their adversarial judges. Recording them
so the effort is not respent:

| Claim | Why it died |
|---|---|
| Vinnova ingestion has no wall-clock bound | A bound exists |
| Adapter API responses buffered with no size cap | Cap exists on that path |
| Untrusted reference/call text reaches the agent with no untrusted framing | Framing exists |
| Cloud-disclosure gate not applied to the citation/reference LLM surface | *See the critic note below — this one deserves a second look* |
| Three git subprocesses in tool handlers have no `timeout=` | Duplicate of 4.x; kept once |
| Turn-reaper TTL hardcoded vs its env-var partner | No harm path |
| Swallowed exception in the lock probe permanently disables scheduled work | Guard exists |
| Stale-source backstop cron fires before the ingest crons it follows | Ordering is intentional |
| Filtered HNSW search has no `ef_search` tuning | Under-recall not demonstrated |
| Shrunk embeddings stored as full-text vectors, zeroed by the 0.20 gate | Refuted |
| Unbounded blocking `git log` on every cold workspace launch | Superseded by 4.5, which is precise |
| No route error boundary: stale lazy chunk takes down the app | Boundary exists |
| Documented SPA deploy deletes the live bundle before its replacement | Procedure is atomic |

Also refuted by the earlier pass and still refuted: the *"workspaces router god
module"* and *"exception-to-HTTP shotgun surgery"* claims.

> **One refutation is contested.** A critic argues the citation-LLM path is a
> **sixth cloud-egress route with no disclosure gate**: `grep -rn gate_cloud_llm
> src/call_manager` returns exactly five call sites, but
> `web/api/citation_audit.py:103-114` takes `require_owner` and then calls
> `validate_citations_semantic_capped(...)` at :137-146 with **no `gate_cloud_llm`,
> no disclosure check, no kill-switch check** — while `citations/llm.py:76-98`
> explicitly logs *"uploaded reference text will be sent to an EXTERNAL API"* when
> `provider == "anthropic"`. The same service also runs unattended on reference
> upload. **This is the single highest-value item to check next**, because if it
> holds, P1-3's entire premise (that the gate protects egress) has a hole in it.

---

## 9. What nobody has read

`src/call_manager` holds **278 `.py` files**. The union of every path cited across
all three reviews is **70**. **178 non-`__init__` modules have never been opened by
any reviewer.**

Per-directory misses: `web/api` 14 · `proposal_assist/references` 14 · `ranking`
13 · `proposal_assist/collab` 13 · `proposal_assist/citations` 12 ·
`proposal_assist/ports` 9 · `auth` 9 · `profiles` 8 · `proposal_assist/drafting` 7
· `digest` 3.

Largest untouched modules: `web/api/workspaces.py` (**1,572 lines — the biggest
module in the app**), `citations/audit.py` (1,027), `citations/bibliography.py`
(808), `turns/processor.py` (676), `collab/markdown_bridge.py` (655),
`collab/kernel.py` (583), `drafting/registry.py` (551).

**The epoch-fence spine — this project's active architectural work — got zero
review, in an architecture review.** Unread: `fencing.py`, `port_impl/effects.py`,
all 9 files under `ports/`, `row_locks.py`, `lifecycle.py`, `jobs_retirement.py`,
`reconcile.py`, and all 27 files under `tests/fencing/`.

**Alembic:** 66 files, zero read. Parsed for this pass: 65 migrations, single head
`0065`, no branch points, three `pass`-only downgrades. `grep -rln
"compare_metadata|autogenerate" tests/` returns **nothing** — there is no drift
gate, which is the missing context behind §5's orphan-index finding.

**Frontend coverage ≈ 6%:** 264 `.ts/.tsx` under `frontend/src`.

Two areas were checked and found **sound**, recorded so nobody respends the effort:

- **Ranking's remote-escalation ledger.** `worker.py:201-212` writes the ledger row
  in its own committed short transaction *before* the main merge, with the
  reasoning spelled out in comments. This is precisely the discipline P1-2 found
  missing in coherence/evaluator — **the in-repo precedent the fix should copy.**
- **Google OIDC login.** PKCE, encrypted txn cookie scoped to `/auth/google`, state
  + nonce, domain allowlist on both legs, `link_epoch` revalidation,
  `email_verified is not True` rejected and tested, and no auto-link on email
  collision. Clean — and it is the primary production login path, on which the
  earlier reviews state no position at all.

---

## 10. Recommended sequence

Ordered by measured harm per unit of effort.

1. **Bound the CRDT sync decoder (P0-1).** Route STEP1/SYNC_UPDATE/`extract_update`
   through a bounded reader, drop `read_message` from `protocol.__all__`, clone the
   existing `< 2.0 s` timing assertion onto STEP1 at the frame cap. This is a
   measured whole-instance outage reachable by any member; nothing else on this
   list is close.
2. **Check the citation-LLM egress path** (§8 contested refutation). If it is a
   sixth ungated cloud path, it changes the shape of P1-3 and of the disclosure
   contract generally.
3. **Fail closed on SSRF refusal** (§4.1) — two one-line changes, and add the test
   that exercises the production branch the suite currently never reaches.
4. **Type the ingestion boundary** (§4.2) — one `str()`/validator prevents a
   permanent silent outage.
5. **Fix the three high-stakes hollow gates** (§4.12–4.14): the two `client_unauth`
   authorization tests, the stubbed disclosure-gate session, and the untested day
   boundary. These are worse than no coverage, because they report green.
6. **Land the spend ledger** (§6.2) instead of patching P1-2's two sites. Then
   backport the `worker.py:201-212` pattern the repo already got right.
7. **Enable ruff `ASYNC`** (§6.3) — 4 fixes, free floor — then write the AST gate
   for the 28 sync-session-in-coroutine sites.
8. **Stop flattening deliberate 5xx detail** (§4.7) and give the eight
   proposal-assist routers the error envelope (§5).
9. **Frontend truthfulness pass** (§4.8–4.10): the `synced` latch, the reconnect
   give-up, and the Versions invalidation. All three lie to the user about state.
10. **Then** the second pass's original list — the Coherence pre-launch check, the
    disclosure error code, the test-lifecycle fixture, the collab orchestrator
    extraction, and the five pending fence slices. They remain correct work; they
    are not the most urgent work.
11. **Give §7's 23 promotions their `file:line` and harm sentence**, or return them
    to backlog. Do not plan against them as they stand.
12. **Add an `alembic check` / `compare_metadata` drift gate** before acting on the
    orphan-index finding, and re-derive that finding against it.

---

## 11. Final assessment

**Architecture grade: B−**, down from B+, on the strength of one measured P0 and
three systemic patterns — not on the strength of the finding count.

The foundations the earlier reviews praised are real: the deterministic ranking
core, the database-backed lifecycle fencing, the source-adapter boundary, the
generated OpenAPI contract, the scheduler ownership lock, the explicit
startup/shutdown lifecycle. The OIDC leg and the ranking escalation ledger are
genuinely well built. A rewrite remains unwarranted.

But the recurring shape of these defects is not drift. It is that **this codebase
knows the right answer and writes it down at one site.** The varuint fix, the spend
accounting, the async session rule, the hermetic git env, the fail-closed transport
— each exists, correct, in one place, with nothing structurally stopping the next
site from getting it wrong. The project already has the tool for this: the
fencing work's AST gates and pinned exemption lists are exactly the right
mechanism. It has simply never been pointed at these three invariants.

The second pass's method — treat each prior claim as a hypothesis and downgrade
what cannot be reproduced — was sound, and every individual downgrade in its §8 is
defensible. Its aggregate conclusion was wrong for a structural reason worth
naming: **it graded the previous review's claims, not the system.** Its scope was
inherited, so *"no verified P0"* was a true statement about three subsystems,
presented as a statement about the codebase. When seven unexamined lenses were
opened, the first one returned a measured whole-instance denial of service.

The lesson is the one this project has already written into its own memory: a
review that converges on a narrowed brief converges to the brief. Close with an
open one.
