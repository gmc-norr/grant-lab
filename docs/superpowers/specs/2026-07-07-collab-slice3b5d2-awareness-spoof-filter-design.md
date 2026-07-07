# Collab Slice 3b-5d·D2 — Awareness Relay-Spoof Filter (Design)

**Date:** 2026-07-07
**Slice:** 3b-5d·D2 (third sub-slice of the flip family; follows D0 e2e de-risk, D1a toolbar robustness, D1b stale-snapshot 409).
**Status:** Design approved; ready for implementation plan.
**Closes:** the 3b-5b **REQUIRED** deferral — "the ACTIVE relay-spoof (high-clock hide/fake a peer's caret) does NOT self-heal → payload-filter DEFERRED to 3b-5d REQUIRED."
**Flip prerequisites remaining after D2:** D3 (post-checkpoint Tier-1/2 analysis re-fire) → D4 (THE FLIP, LAST).

---

## 1. Goal

Prevent a connected collab client from **hiding, overriding, or displacing another user's presence** (caret) via the awareness relay. Today the server relays awareness payloads **raw** (`room.py` awareness branch), so an authenticated workspace member can send an awareness frame carrying a *peer's* client-id at a high clock. 3b-5b #2 closed only the *disconnect-removal* vector; the active relay-spoof is this slice.

**Mechanism (corrected against y-protocols@1.0.7 `awareness.js`; the earlier draft was half-right).** y-awareness is clock-gated (`applyAwarenessUpdate` applies an entry only if `currClock < clock`, or an equal-clock null removal). Two spoof variants, with *different* natural outcomes:

- **High-clock REMOVAL of a peer's id (the "hide"):** `applyAwarenessUpdate` has an **own-id defense** (`awareness.js:259-262`) — a received removal of *your own* live state is not applied; instead your local clock is bumped past the spoof. So a **one-shot** removal self-heals at the victim's next renewal (≤ ~15 s). Sustained hiding needs a **spam loop** re-outbidding each renewal. Nuance: our provider suppresses the *immediate* defensive re-announce that y-websocket would relay (it arrives with `origin === this` → guarded at `collabProvider.ts:72`), so the one-shot flicker widens from ~RTT to ≤ ~15 s.
- **High-clock non-null OVERRIDE of a peer's id (the "fake"):** there is **no own-id defense branch** for non-null state (`awareness.js:266`), so the attacker's junk state replaces the victim's own local state at the spoofed clock; the victim then re-broadcasts the *poisoned* state under its own renewals (a fake name/color persists; only the cursor position is re-set on the victim's next movement). This variant genuinely does not self-heal.

D2's filter kills **both** variants before anyone — the victim included — ever receives them, so the design is unchanged by this correction; but future slices (identity-from-binding, §8) will reason from this mechanism model, so it is stated accurately here.

## 2. Threat model and scope decisions (from review discussion)

Presence is ephemeral, never persisted, display-only, and gates nothing. All durable attribution (yjs-update actor tags, ArtifactVersion co-authors, WorkspaceEvents, proposals, citations) is server-stamped from the authenticated session and untouchable via awareness. Attackers are authenticated workspace members (connect requires membership + Origin check).

| # | Attack | Effect | D2 disposition |
|---|--------|--------|----------------|
| 1 | Clock-spoof a live peer's id | **Silences** the victim's caret; no self-heal | **Closed** (ownership filter) |
| 2 | Claim a blipped peer's id (ownership inversion) | A live-socket-ownership filter would silence the *victim* on reconnect | **Closed** (room-lifetime **user** binding) |
| 3 | Fake name/color on own id | Duplicate caret with a false label; silences nobody; audit trail untouched | **Documented limitation** (§8; future path sketched) |
| 4 | Binding-spam (many fresh ids) | Ghost carets; unbounded map growth | **Bounded + attributable** (per-user quota; own-bound junk carets below quota remain — cosmetic, audit-logged, same class as #3) |
| 5 | Junk state JSON | Cosmetic | Already handled (frame caps, drop-on-malformed, React/CollaborationCaret render as text) |

**Decisions made with the user:**
- Scope = id-ownership filter only. Server-stamped identity (rewriting `user.name`/`user.color` in state payloads) was considered and **rejected for D2**: it is JSON parse+rewrite on the hottest relay path of a single-instance event loop (the proven DoS-sensitive spot, cf. f535f6c), couples the server to CollaborationCaret's private state shape, and must replicate the frontend's display-name/color derivation byte-for-byte — drift mislabels (or with validate-and-drop, **silences**) every legitimate user.
- Attack #3 is not flip-blocking for an internal hospital tool with a full server-side audit trail; the sanctioned future fix is **identity-from-binding** (§8), for which D2's binding map is deliberately the foundation.

## 3. Invariant

> **A socket may introduce, update, or remove awareness entries only for client-ids bound to its own authenticated user.** Binding is first-announce, per-**user** (not per-socket), for the **room's lifetime**; destroyed with the room (evict-on-empty).

Room-lifetime user binding is what kills attack #2: a griefer cannot claim a blipped peer's id (the binding outlives the victim's socket), while the victim's same-user reconnect re-claims it freely — preserving 3b-5b R2-2 semantics (same-user re-attribution) exactly.

Backend-only. **No migration.** Flag stays **OFF** (the awareness path is reachable only on a collab-enabled artifact → prod-inert until D4). No new locks; all new work sits inside the existing `room._lock` section of the awareness branch; broadcast stays outside the lock.

## 4. How legitimate traffic interacts with the filter (verified against y-protocols@1.0.7)

**Legitimate clients DO emit foreign-id frames — routinely — and that is fine; they are filtered as benign stale-scrubs, not flagged as spoofs.** Every y-protocols `Awareness` runs a 3 s `_checkInterval` (`awareness.js:59-77`) that locally removes any remote id not renewed for 30 s via `removeAwarenessStates(this, remove, 'timeout')`. Origin `'timeout'` ≠ `this`, so the provider's origin guard (`collabProvider.ts:72`) does **not** suppress it, and the provider ships a frame whose only entries are foreign ids (null state, at the victim's current clock). The common benign trigger is **browser timer throttling**: a backgrounded cockpit tab's renewal stretches past 60 s, so every *other* client scrubs its caret and emits a foreign-removal frame ~once per cycle, all day.

**These frames are correctly filtered** (not relayed): a high-clock foreign removal is precisely the "hide" attack, indistinguishable by intent from a benign scrub, and each peer runs its own local 30 s scrub so the relay is redundant. (Small win: today a tab waking from sleep can relay an equal-clock scrub that flickers a live peer's caret off *for everyone*; post-D2 it cannot.) The filter therefore treats **all** foreign entries alike (drop from relay); the *logging* distinguishes them (§7): foreign **null-state** = expected stale-scrub (INFO), foreign **non-null** = the true spoof signature (WARNING).

The other three legitimate paths carry **only own-id** entries (verified):
- Reconnect-cleanup (`collabProvider.ts:190`, `removeAwarenessStates(remote, this)`) uses origin `this` → guard suppresses the send.
- Destroy / on-open flushes are own-id-only (`collabProvider.ts:103,148`).
- Server-originated removals (`Room.remove_client_presence`, dead-socket scrub in `_broadcast`) do not pass through `Room.handle`.
- Yjs `Doc.clientID` is random per Doc instance: page reload → fresh id (binds fresh); network-blip reconnect → same provider/doc → same id, same user → allowed by binding.

## 5. Component A — `protocol.py`: entry-span parser + filter

**File:** `src/call_manager/proposal_assist/collab/protocol.py`

1. **`read_awareness_entries(payload: bytes) -> list[AwarenessEntry]`** — refactor of the existing hardened parser. `AwarenessEntry` is a small `NamedTuple(client_id: int, start: int, end: int, is_null: bool)`, where `start:end` is the entry's full byte span (clientID varuint + clock varuint + varString state), and `is_null` is `payload[state_start:state_end] == b"null"` (a null-state removal — `JSON.stringify(null) === "null"`; a 4-byte compare on the span already in hand, no JSON decode). Preserves **all** the f535f6c hardening verbatim: 10-byte varuint cap, bounds-checked reads, O(payload), prompt `ValueError` on truncated/overlong/out-of-range.
2. **`read_awareness_client_ids(payload) -> list[int]`** — becomes a thin wrapper: `[e.client_id for e in read_awareness_entries(payload)]`. One parser, no drift; existing tests keep passing.
3. **`filter_awareness_update(payload: bytes, entries: list[AwarenessEntry], allowed_ids: set[int]) -> bytes | None`**:
   - every entry's id ∈ `allowed_ids` → return `payload` **unchanged, byte-identical** (fast path; the overwhelmingly common case — zero re-encode in normal operation);
   - no entry allowed → `None`;
   - mixed → rebuild: `_write_varuint(kept_count)` + concatenation of the kept entries' original byte spans (entries are never re-encoded, only re-counted).
   - Includes a tiny `_write_varuint(n: int) -> bytes` helper (base-128, mirrors the reader).

## 6. Component B — `room.py`: awareness branch rework

**File:** `src/call_manager/proposal_assist/collab/room.py`

**New Room state:** `self._awareness_bindings: dict[int, int]` (awareness client_id → user_id). Module constant **`_MAX_AWARENESS_BINDINGS_PER_USER = 64`** — a **per-user** quota (legit usage ≈ 1 binding per page-load; 64 is generous). Per-user, NOT per-room: a per-room cap is an abuse amplifier — one member could burn a 512-id room cap with a single ~5 KB frame, after which *every future joiner's* first-announce (a new binding) is whole-dropped → all newcomers presence-silent for the room's life (attack #1's effect, achieved *through* the mitigation). Per-user quota confines a spammer to exhausting only *their own* presence; total map growth is bounded by `members × 64`.

**Awareness branch, order (parse OUTSIDE the lock, as today — nothing needs the lock until the bindings read; keeps the critical section minimal):**

1. Parse (OUTSIDE the lock): `entries = P.read_awareness_entries(payload)` (exception → drop + existing `collab.malformed_awareness` log — unchanged).
2. Enter `async with self._lock:` for steps 3–8.
3. Partition by `allowed(id) := self._awareness_bindings.get(id) in (None, state.user_id)` → `kept` / `filtered` (keep each side's entries, not just ids, so the log can split null vs non-null).
4. **Log filtered entries by kind** (§7) — BEFORE the quota, so a mixed quota+spoof frame still fires the spoof signal (Codex plan-review, Medium): foreign **null-state** entries → INFO `collab.awareness_stale_scrub_filtered` (expected — the timeout-scrub of §4); foreign **non-null** entries → WARNING `collab.awareness_foreign_filtered` (room, sender user_id, ids — the true spoof signature).
5. If `kept` is empty (and the frame declared entries) → **return** (nothing applied, nothing relayed). This is the pure-stale-scrub common case.
6. **Per-user quota:** among `kept`, the ids not yet in the map are *prospective* bindings; if binding them all would push this user past `_MAX_AWARENESS_BINDINGS_PER_USER` → WARNING `collab.awareness_bindings_capped` (room, user_id, counts) and drop ONLY those new-id entries from `kept` — already-bound renewals (heartbeats/caret moves) always pass (Codex Medium; a whole-frame drop would lose the renewal riding in the same frame). If nothing is left, return.
7. `relay = P.filter_awareness_update(payload, entries, kept_ids)`; **apply the FILTERED payload** to `self.awareness` (`apply_awareness_update(relay, origin="client")`; exception → drop + existing log, **nothing bound**). This also fixes the currently-unnoticed hole where a spoofed entry poisons `awareness_snapshot()` for every future joiner.
8. **Bind AFTER accept (Codex plan-review, High):** bind (+ INFO `collab.awareness_bind` — the impersonation-complaint audit trail) only the new ids pycrdt actually **accepted into `awareness.states`**. A parse-valid entry that fails on apply (malformed JSON state) or is clock-ignored (e.g. clock 0) must NOT reserve a binding — bind-on-parse would let a *stealth pre-claim* (no visible caret) filter the id's real owner for the room's life. **Attribution:** `state.awareness_ids |=` (kept ids **bound to this user**) — an unbound-never-accepted id (e.g. a null pre-claim) is never attributed, so a socket's disconnect can never scrub an id someone else binds later (else the 3b-5b griefing vector would be resurrected through the new code). The 3b-5b `owned_by_other_users` computation is **removed — subsumed by bindings**. (The old `declared ∪ states-diff` union collapses: after a filtered apply, states-diff ⊆ kept_ids.)
9. **Relay the FILTERED payload** OUTSIDE the lock (as today; steps 5–6 already returned when nothing is kept).

**Removal entries (null state)** follow the same rule: own-bound id → allowed (legit self-removal on unload — provider destroy path); foreign-bound id → filtered (that IS the hide-attack). An unbound-id removal is allowed through and relayed (harmless — receivers scrub a nonexistent or already-stale entry) but **binds and attributes nothing**: a removal never enters `states`, so bind-after-accept skips it by construction.

**Empty frames** (count = 0): trivially "all allowed" → fast path relays unchanged (behavior-preserving vs today).

**Binding lifecycle:** bindings are never released while the room lives (the point — prevents inversion); the map dies with the Room object on eviction (evict-on-empty, force-evict, poison recovery — all end in the room being dropped from the registry). Rooms hydrate with a fresh `Awareness` (presence never persisted) → no seeding needed.

## 7. Logging & observability summary

| Event | Level | When |
|---|---|---|
| `collab.awareness_bind` | INFO | an unbound client_id binds to a user (audit trail) |
| `collab.awareness_stale_scrub_filtered` | INFO | foreign **null-state** entry filtered — the routine timeout-scrub of §4 (expected; frequent for backgrounded tabs) |
| `collab.awareness_foreign_filtered` | WARNING | foreign **non-null** entry filtered — the true spoof signature (should be rare in practice) |
| `collab.awareness_bindings_capped` | WARNING | frame dropped at the per-user binding quota |
| existing `collab.malformed_awareness*` | WARNING | unchanged |

The level split is load-bearing: without it, the routine stale-scrub (§4) fires the same WARNING all day for every backgrounded tab and drowns the exact spoof signal the WARNING is meant to carry.

## 8. Documented limitations & sanctioned future path

- **#3 Name/color impersonation on own id — NOT closed.** The caret's `user.{name,color}` is client-supplied JSON; a member can present a false label *on their own id* (a visible duplicate caret — it silences nobody, and durable attribution is server-stamped). **Future fix (post-flip, optional slice): identity-from-binding** — the server sends the room's binding map (client_id → authenticated user identity) to clients via a new message type, and the caret renders identity from the server's binding instead of the client payload. Identity by authentication, not by claim; zero payload rewriting; zero display-logic drift risk. D2's binding map is deliberately its foundation. Interim mitigation: the `collab.awareness_bind` audit trail resolves any impersonation complaint.
- **3b-5b #3 reconnect-clock brief-invisibility** (same-clock re-announce ignored until renewal ≤ ~15 s) — unchanged, still documented-not-fixed.
- **Same-user two-socket overlap** (R2-2): both sockets may carry the same id in `awareness_ids`; the first socket's disconnect briefly scrubs the survivor's caret → self-heals via renewal. Pre-existing, unchanged.
- **Binding-reset race** (new residual): bindings die with the Room. After a force-evict/poison-recovery (all sockets closed, everyone auto-reconnects ~1 s later) or an evict-on-empty during a victim's transient blip, the map is fresh — a malicious member who reconnects first can *pre-claim* a peer's known client-id, and the peer's same-Doc re-announce is then filtered until a reload mints a fresh id. Low likelihood (needs a prior sighting of the id + winning the reconnect race), attributable via the `collab.awareness_bind` log, and strictly better than today's no-filter. Accepted, documented.
- **Half-open-socket ghost** (new residual): today a dead-but-undetected peer is scrubbed from server awareness by the first *relayed* client timeout-scrub; post-D2 those foreign scrubs are filtered, so a new joiner can briefly see a dead caret (≤ 30 s, until their own local timeout clears it). Accepted. **Do NOT "fix" this by applying foreign removals server-side without relaying** — pycrdt's `apply_awareness_update` is clock-gated too, so an attacker's `clock=1e9` removal would then *permanently* evict a live victim from every future joiner's snapshot. That cure is worse than the disease; the filter-and-drop chosen here is deliberate.

## 9. Testing

**Protocol unit tests** (`tests/proposal_assist/collab/` — alongside the existing parser tests):
- `read_awareness_entries` spans are exact (reconstructing `payload[start:end]` per entry and re-concatenating with the count reproduces the payload).
- Hardening preserved: truncated varuint, overlong (>10-byte) varuint, lying count, out-of-range varString → prompt `ValueError` (reuse/extend the existing malformed cases against the new function).
- `filter_awareness_update`: all-allowed → **byte-identical** payload (`is`/`==` on bytes); none-allowed → `None`; mixed → rebuilt payload parses to exactly the kept ids AND applies cleanly to a fresh `pycrdt.Awareness`.
- `read_awareness_client_ids` wrapper equivalence on conforming frames.

**Room tests** (extend `tests/proposal_assist/collab/test_collab_awareness.py`, FakeClient pattern):
- **Spoof-hide blocked (THE regression):** user A announces id X; user B sends a high-clock/removal entry for X → frame NOT relayed to peers, server awareness still holds A's state, `awareness_snapshot()` to a new joiner is spoof-free, **WARNING** logged (non-null override case), X not in B's `awareness_ids`.
- **Foreign null-state scrub filtered benignly (finding 1):** B sends a *null-state* entry for A's id X **at X's current clock** (the routine timeout-scrub shape) → filtered (not relayed), A's state intact, logged at **INFO** (`collab.awareness_stale_scrub_filtered`), NOT WARNING. Guards the log-level split.
- **Mixed frame stripped:** B sends [own id Y, foreign id X] → peers receive a frame whose parsed ids == [Y]; server awareness updated for Y only; Y (not X) attributed to B.
- **Inversion blocked:** A announces X; A's socket drops (`drop_client`); B announces X → filtered (binding outlives the socket); A reconnects (same user, new socket) and announces X → allowed, relayed, attributed.
- **First-announce binds:** unknown id → allowed, bound, relayed, INFO logged.
- **Fast path:** an all-own frame relays a byte-identical payload.
- **Empty-frame passthrough (count = 0):** an awareness frame declaring zero entries → trivially all-allowed → relayed byte-identical, no bindings created (behavior-preserving vs today).
- **Quota isolation (finding 2):** user B announces `_MAX_AWARENESS_BINDINGS_PER_USER` ids (fills B's quota), then one more → B's new id dropped + WARNING; **user C's fresh first-announce still binds and relays** (C unaffected). Asserts the per-user (not per-room) property.
- **Quota preserves renewals + foreign logging (Codex Medium):** at the cap, a mixed frame [bound renewal + over-cap new id + foreign spoof] → the renewal alone relays (and updates server state), the new id is dropped + capped-WARNING, and the foreign entry STILL fires `collab.awareness_foreign_filtered`.
- **Failed apply reserves no binding (Codex High):** B sends a span-valid frame with invalid-JSON state for unbound id X → apply raises, X NOT bound, nothing relayed; A's later announce of X binds to A and relays.
- **Clock-ignored entry reserves no binding (Codex High):** B sends a valid clock-0 entry for unbound X → apply is a no-op, X NOT bound and not in states; A's later announce binds to A.
- **Unbound-null pre-claim is not attributed:** B sends a null entry for unbound X → relayed but neither bound nor attributed to B's socket; after A binds X, B's departure (`remove_client_presence`) cannot scrub A's caret.
- **Self-removal still works:** A sends a null-state entry for its own id → allowed, relayed; peers' view drops the caret.
- **3b-5b legit-flow tests green unchanged** (disconnect-removal, same-user reclaim R2-2, malformed-frame drop).

**Acceptance gates:** grouped backend sweep incl. `tests/test_api_spec.py` (`EMBEDDING_MODEL=bge-m3`, `-p no:cacheprovider`, never whole-suite) · `ruff check .` · frontend `pnpm exec tsc --noEmit` + `pnpm exec vitest run src/collab` (untouched — regression only) · **two-browser e2e suite (a–e)** via `scripts/collab_e2e_run.sh` as the legitimate-presence regression gate (scenarios a/b/d hammer the relay: converge + distinct carets, reconnect re-announce, read-only spec presence).

## 10. Global constraints (verbatim)

- Feature flag **stays OFF** in prod until the explicit 3b-5d flip (D4).
- **No migration, no schema change** for collab slices. Backend-only; frontend untouched.
- All parsing preserves the f535f6c DoS bounds (capped varuints, bounds-checked, O(payload)).
- No new locks; no lock-ordering changes (work sits inside the existing awareness-branch `room._lock` section; broadcast outside).
- Explicit-path `git add` only; merges are **local `--no-ff` only**, not pushed without an explicit ask; do NOT invoke `/code-review` / `/ultrareview`; Codex findings are authoritative-but-verify-against-code.
- Commit messages end with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## 11. Key references

- `src/call_manager/proposal_assist/collab/room.py:186-232` — the awareness branch (raw relay at 230-231; the 3b-5b comment at 217-223 explicitly tracks this fix).
- `src/call_manager/proposal_assist/collab/protocol.py:81-126` — `read_awareness_client_ids` (the hardened parser to refactor).
- `frontend/src/collab/collabProvider.ts:69-75` — the origin-tag guard proving legit frames are own-id-only.
- `tests/proposal_assist/collab/test_collab_awareness.py` — existing awareness tests (3b-4 + 3b-5b).
- 3b-5b findings: `docs/superpowers/specs/2026-07-05-collab-slice3b5b-presence-followups-design.md` (the REQUIRED deferral).
