# Collab Slice 3b-5d·D2 — Awareness Relay-Spoof Filter (Design)

**Date:** 2026-07-07
**Slice:** 3b-5d·D2 (third sub-slice of the flip family; follows D0 e2e de-risk, D1a toolbar robustness, D1b stale-snapshot 409).
**Status:** Design approved; ready for implementation plan.
**Closes:** the 3b-5b **REQUIRED** deferral — "the ACTIVE relay-spoof (high-clock hide/fake a peer's caret) does NOT self-heal → payload-filter DEFERRED to 3b-5d REQUIRED."
**Flip prerequisites remaining after D2:** D3 (post-checkpoint Tier-1/2 analysis re-fire) → D4 (THE FLIP, LAST).

---

## 1. Goal

Prevent a connected collab client from **hiding, overriding, or displacing another user's presence** (caret) via the awareness relay. Today the server relays awareness payloads **raw** (`room.py` awareness branch), so an authenticated workspace member can send an awareness frame carrying a *peer's* client-id at a high clock — y-awareness is clock-gated, so the victim's genuine lower-clock re-announces are discarded by all receivers and the spoof **does not self-heal** (not even via the 15 s heartbeat). 3b-5b #2 closed only the *disconnect-removal* vector; the active relay-spoof is this slice.

## 2. Threat model and scope decisions (from review discussion)

Presence is ephemeral, never persisted, display-only, and gates nothing. All durable attribution (yjs-update actor tags, ArtifactVersion co-authors, WorkspaceEvents, proposals, citations) is server-stamped from the authenticated session and untouchable via awareness. Attackers are authenticated workspace members (connect requires membership + Origin check).

| # | Attack | Effect | D2 disposition |
|---|--------|--------|----------------|
| 1 | Clock-spoof a live peer's id | **Silences** the victim's caret; no self-heal | **Closed** (ownership filter) |
| 2 | Claim a blipped peer's id (ownership inversion) | A live-socket-ownership filter would silence the *victim* on reconnect | **Closed** (room-lifetime **user** binding) |
| 3 | Fake name/color on own id | Duplicate caret with a false label; silences nobody; audit trail untouched | **Documented limitation** (§8; future path sketched) |
| 4 | Binding-spam (many fresh ids) | Ghost carets; unbounded map growth | **Closed** (binding cap) |
| 5 | Junk state JSON | Cosmetic | Already handled (frame caps, drop-on-malformed, React/CollaborationCaret render as text) |

**Decisions made with the user:**
- Scope = id-ownership filter only. Server-stamped identity (rewriting `user.name`/`user.color` in state payloads) was considered and **rejected for D2**: it is JSON parse+rewrite on the hottest relay path of a single-instance event loop (the proven DoS-sensitive spot, cf. f535f6c), couples the server to CollaborationCaret's private state shape, and must replicate the frontend's display-name/color derivation byte-for-byte — drift mislabels (or with validate-and-drop, **silences**) every legitimate user.
- Attack #3 is not flip-blocking for an internal hospital tool with a full server-side audit trail; the sanctioned future fix is **identity-from-binding** (§8), for which D2's binding map is deliberately the foundation.

## 3. Invariant

> **A socket may introduce, update, or remove awareness entries only for client-ids bound to its own authenticated user.** Binding is first-announce, per-**user** (not per-socket), for the **room's lifetime**; destroyed with the room (evict-on-empty).

Room-lifetime user binding is what kills attack #2: a griefer cannot claim a blipped peer's id (the binding outlives the victim's socket), while the victim's same-user reconnect re-claims it freely — preserving 3b-5b R2-2 semantics (same-user re-attribution) exactly.

Backend-only. **No migration.** Flag stays **OFF** (the awareness path is reachable only on a collab-enabled artifact → prod-inert until D4). No new locks; all new work sits inside the existing `room._lock` section of the awareness branch; broadcast stays outside the lock.

## 4. Why legitimate traffic is unaffected (verified against source)

- Our provider (`frontend/src/collab/collabProvider.ts:72`) has an origin-tag guard — it **never echoes** remote-applied awareness updates; every legitimate frame contains only the sender's own client-id entries. Any foreign-id entry at the server is anomalous by construction.
- Reconnect-cleanup (`collabProvider.ts:190`, `removeAwarenessStates(remote, this)`) uses origin `this` → the guard suppresses the send; no foreign-id frames from that path either.
- Server-originated removals (`Room.remove_client_presence`, dead-socket scrub in `_broadcast`) do not pass through `Room.handle` — untouched.
- Yjs `Doc.clientID` is random per Doc instance: page reload → fresh id (binds fresh); network-blip reconnect → same provider/doc → same id, same user → allowed by binding.

## 5. Component A — `protocol.py`: entry-span parser + filter

**File:** `src/call_manager/proposal_assist/collab/protocol.py`

1. **`read_awareness_entries(payload: bytes) -> list[tuple[int, int, int]]`** — refactor of the existing hardened parser. Returns `(client_id, start, end)` per entry, where `start:end` is the entry's full byte span (clientID varuint + clock varuint + varString state). Preserves **all** the f535f6c hardening verbatim: 10-byte varuint cap, bounds-checked reads, O(payload), prompt `ValueError` on truncated/overlong/out-of-range.
2. **`read_awareness_client_ids(payload) -> list[int]`** — becomes a thin wrapper: `[e[0] for e in read_awareness_entries(payload)]`. One parser, no drift; existing tests keep passing.
3. **`filter_awareness_update(payload: bytes, entries: list[tuple[int, int, int]], allowed_ids: set[int]) -> bytes | None`**:
   - every entry's id ∈ `allowed_ids` → return `payload` **unchanged, byte-identical** (fast path; the overwhelmingly common case — zero re-encode in normal operation);
   - no entry allowed → `None`;
   - mixed → rebuild: `_write_varuint(kept_count)` + concatenation of the kept entries' original byte spans (entries are never re-encoded, only re-counted).
   - Includes a tiny `_write_varuint(n: int) -> bytes` helper (base-128, mirrors the reader).

## 6. Component B — `room.py`: awareness branch rework

**File:** `src/call_manager/proposal_assist/collab/room.py`

**New Room state:** `self._awareness_bindings: dict[int, int]` (awareness client_id → user_id). Module constant `_MAX_AWARENESS_BINDINGS = 512` (per room; one binding per page-load per user — generous; bounds the map against spam, in the spirit of the f535f6c DoS bounds).

**Awareness branch, new order (all under the existing `self._lock`):**

1. Parse: `entries = P.read_awareness_entries(payload)` (exception → drop + existing `collab.malformed_awareness` log — unchanged).
2. Partition by `allowed(id) := self._awareness_bindings.get(id) in (None, state.user_id)` → `kept_ids` / `filtered_ids`.
3. **Cap check:** if the number of *new* bindings (`kept_ids` not yet in the map) would push `len(self._awareness_bindings)` past `_MAX_AWARENESS_BINDINGS` → drop the whole frame + WARNING `collab.awareness_bindings_capped` (room, sender user_id). Existing presence unaffected.
4. **Bind** new ids to `state.user_id`; INFO `collab.awareness_bind` (room, client_id, user_id) — this is the impersonation-complaint audit trail.
5. If `kept_ids` is empty → WARNING `collab.awareness_foreign_filtered` (room, sender user_id, filtered ids) + **return** — nothing applied, nothing relayed.
6. `relay_payload = P.filter_awareness_update(payload, entries, kept_ids)`; if `filtered_ids` non-empty → the same WARNING log (kept/filtered counts).
7. **Apply the FILTERED payload** to `self.awareness` (`apply_awareness_update(relay_payload, origin="client")`; exception → drop + existing log). This also fixes the currently-unnoticed hole where a spoofed entry poisons `awareness_snapshot()` for every future joiner.
8. **Attribution simplifies:** `state.awareness_ids |= kept_ids`. The 3b-5b `owned_by_other_users` computation is **removed — subsumed by bindings** (an id bound to another user is filtered before it could ever be attributed), so the disconnect-removal guard becomes structurally unable to scrub a foreign caret. (The old `declared ∪ states-diff` union collapses: after a filtered apply, the states-diff ⊆ kept_ids.)
9. **Relay the FILTERED payload** outside the lock (as today; skip if nothing kept — step 5 already returned).

**Removal entries (null state)** follow the same rule: own-bound or unbound id → allowed (legit self-removal on unload — provider destroy path); foreign-bound id → filtered (that IS the hide-attack). An unbound-id removal binds then removes — harmless, uniform.

**Empty frames** (count = 0): trivially "all allowed" → fast path relays unchanged (behavior-preserving vs today).

**Binding lifecycle:** bindings are never released while the room lives (the point — prevents inversion); the map dies with the Room object on eviction (evict-on-empty, force-evict, poison recovery — all end in the room being dropped from the registry). Rooms hydrate with a fresh `Awareness` (presence never persisted) → no seeding needed.

## 7. Logging & observability summary

| Event | Level | When |
|---|---|---|
| `collab.awareness_bind` | INFO | an unbound client_id binds to a user (audit trail) |
| `collab.awareness_foreign_filtered` | WARNING | ≥1 entry filtered (should never fire for our provider — origin guard §4) |
| `collab.awareness_bindings_capped` | WARNING | frame dropped at the binding cap |
| existing `collab.malformed_awareness*` | WARNING | unchanged |

## 8. Documented limitations & sanctioned future path

- **#3 Name/color impersonation on own id — NOT closed.** The caret's `user.{name,color}` is client-supplied JSON; a member can present a false label *on their own id* (a visible duplicate caret — it silences nobody, and durable attribution is server-stamped). **Future fix (post-flip, optional slice): identity-from-binding** — the server sends the room's binding map (client_id → authenticated user identity) to clients via a new message type, and the caret renders identity from the server's binding instead of the client payload. Identity by authentication, not by claim; zero payload rewriting; zero display-logic drift risk. D2's binding map is deliberately its foundation. Interim mitigation: the `collab.awareness_bind` audit trail resolves any impersonation complaint.
- **3b-5b #3 reconnect-clock brief-invisibility** (same-clock re-announce ignored until renewal ≤ ~15 s) — unchanged, still documented-not-fixed.
- **Same-user two-socket overlap** (R2-2): both sockets may carry the same id in `awareness_ids`; the first socket's disconnect briefly scrubs the survivor's caret → self-heals via renewal. Pre-existing, unchanged.

## 9. Testing

**Protocol unit tests** (`tests/proposal_assist/collab/` — alongside the existing parser tests):
- `read_awareness_entries` spans are exact (reconstructing `payload[start:end]` per entry and re-concatenating with the count reproduces the payload).
- Hardening preserved: truncated varuint, overlong (>10-byte) varuint, lying count, out-of-range varString → prompt `ValueError` (reuse/extend the existing malformed cases against the new function).
- `filter_awareness_update`: all-allowed → **byte-identical** payload (`is`/`==` on bytes); none-allowed → `None`; mixed → rebuilt payload parses to exactly the kept ids AND applies cleanly to a fresh `pycrdt.Awareness`.
- `read_awareness_client_ids` wrapper equivalence on conforming frames.

**Room tests** (extend `tests/proposal_assist/collab/test_collab_awareness.py`, FakeClient pattern):
- **Spoof-hide blocked (THE regression):** user A announces id X; user B sends a high-clock/removal entry for X → frame NOT relayed to peers, server awareness still holds A's state, `awareness_snapshot()` to a new joiner is spoof-free, WARNING logged, X not in B's `awareness_ids`.
- **Mixed frame stripped:** B sends [own id Y, foreign id X] → peers receive a frame whose parsed ids == [Y]; server awareness updated for Y only; Y (not X) attributed to B.
- **Inversion blocked:** A announces X; A's socket drops (`drop_client`); B announces X → filtered (binding outlives the socket); A reconnects (same user, new socket) and announces X → allowed, relayed, attributed.
- **First-announce binds:** unknown id → allowed, bound, relayed, INFO logged.
- **Fast path:** an all-own frame relays a byte-identical payload.
- **Cap:** a frame that would exceed `_MAX_AWARENESS_BINDINGS` → dropped + WARNING; previously-bound presence unaffected.
- **Self-removal still works:** A sends a null-state entry for its own id → relayed; peers' view drops the caret.
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
