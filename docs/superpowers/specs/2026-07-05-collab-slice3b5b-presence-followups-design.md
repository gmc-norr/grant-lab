# Collab Slice 3b-5b — Presence Follow-ups (Design)

**Date:** 2026-07-05
**Status:** Design — pending user review
**Builds on:** 3b-4 presence/cursors (`b6e60e3`), 3b-5a citation-insert parity (`9832210`)
**Feature flag:** collaborative editing STAYS OFF in prod. This slice does **not** flip it (that is 3b-5d, the keystone). Presence is ungated/ephemeral (never persisted), so every change here is inert in prod until the flip.

---

## 1. Context & Scope

Slice 3b-4 shipped live presence (server-side pycrdt `Awareness` relay + per-socket id attribution + disconnect removal + Tiptap `CollaborationCaret`). Its review surfaced four presence robustness follow-ups. This slice closes three and formally documents the fourth as an accepted limitation.

The four items (numbering preserved from the 3b-4 review):

| # | Issue | Disposition here |
|---|-------|------------------|
| 1 | "Anonymous" caret flash — a joining client broadcasts a fallback identity for one render before `useSession()` resolves | **FIX** (frontend) |
| 2 | Declared-attribution griefing — a malicious socket can declare a *live peer's* awareness id, so its own disconnect scrubs the peer's caret for everyone | **FIX** (backend) |
| 3 | Reconnect clock-collision — a same-clientID reconnect's caret can be briefly invisible to peers until its awareness clock overtakes the removal clock | **DOCUMENT** (accepted; inherent to y-awareness) |
| 4 | Malformed-awareness JS-side hardening had no test — the provider already drops malformed inbound frames, but the drop was uncovered | **TEST-ONLY** (frontend) |

**Non-goals:** the flag flip, CM6→Tiptap cutover, prod CSP `wss` allowance, active-session tail-bounding, and the 3b-5a flag-on deferrals (C#3 stale-snapshot etc.) — all belong to later slices (3b-5c / 3b-5d).

## 2. Global Constraints (verbatim; apply to every task)

- Feature flag STAYS OFF; no `enable_collab` source caller added. Flag-off behaviour must be byte-for-byte unchanged (this slice touches only the ungated presence path + a frontend render gate that is only reachable when the collab editor renders, i.e. flag-on).
- **No migration, no schema change.** Presence is ephemeral; nothing here persists.
- Backend changes are additive and confined to the presence relay path (`room.py`) — no change to the doc write/persist path, the kernel, or any HTTP endpoint.
- Python 3.12 / `uv` / `ruff` / `pytest` (+ testcontainers Postgres). Frontend: React 19 + Vite + vitest + `pnpm`.
- Run backend tests grouped-dir with `EMBEDDING_MODEL=bge-m3` and `-p no:cacheprovider`; ALWAYS include `tests/test_api_spec.py`. Never run the whole suite (testcontainers PG drops under load).
- CI gate is `ruff check .` (not `ruff format`). Frontend gate is `tsc` + `vitest` (no separate TS linter).
- Explicit-path `git add` only (never `-A`). Detached SDD stops before merge. Merges are LOCAL, not pushed, without an explicit ask.

## 3. Fix #1 — "Anonymous" caret flash (frontend)

### Problem

`CollabEditorInner` seeds `CollaborationCaret` with `displayName(me?.user)`, which is `"Anonymous"` until `useSession()` resolves ([CollabEditor.tsx:47,58](../../../frontend/src/collab/CollabEditor.tsx#L47)). Mounting the caret sets the provider's local awareness state, which fires `onAwarenessUpdate` → `sendAwareness` (and/or the on-open flush at [collabProvider.ts:146-149](../../../frontend/src/collab/collabProvider.ts#L146)) → **peers briefly see a caret labelled "Anonymous"** before the R2-1 `updateUser` effect corrects it. The existing test [CollabEditor.identity.test.tsx:45](../../../frontend/src/collab/CollabEditor.identity.test.tsx#L45) currently *asserts* this pre-resolve "Anonymous" local state — that assertion is exactly the flash we are removing.

In practice `me` is fetched at the app shell (auth-gated) and is almost always already cached in TanStack Query before the cockpit — much less the collab editor — mounts, so the flash is a rare sub-frame window. The fix must therefore be **zero-behaviour-change in the common (cached) path** and only affect the rare pending window.

### Mechanism — extend the outer placeholder gate to require identity

`CollabEditor` (outer) already builds `doc`/`provider` in an effect and renders a `Connecting…` placeholder until both exist, only then mounting `CollabEditorInner` ([CollabEditor.tsx:33-40](../../../frontend/src/collab/CollabEditor.tsx#L33)). We extend that same gate to also require a resolved `me.user`, and pass `me` down to `Inner` as a prop:

```tsx
export function CollabEditor(props: CollabEditorProps) {
  const { doc, provider, status } = useCollabDoc(props);
  const { data: me } = useSession();          // moved up from Inner
  useEffect(() => () => resetEditorStore(), []);
  if (!doc || !provider || !me?.user) {        // gate now also waits for identity
    return (
      <div data-collab-status={status} data-testid="collab-connecting">
        Connecting…
      </div>
    );
  }
  return <CollabEditorInner {...props} doc={doc} provider={provider} status={status} me={me} />;
}
```

`CollabEditorInner` takes `me` via props (guaranteed non-null), so `useEditor` seeds `CollaborationCaret` with the real name/color **from first mount** — the caret's local awareness state is never "Anonymous", so nothing anonymous is ever broadcast. `useEditor` stays unconditional (no hooks-rule violation); no new component layer is introduced.

Why this over a provider-level `identityReady` flag (the "heavier" alternative): the provider flag would keep the editor content visible while suppressing the awareness *send*, but it adds provider surface (a flag + a `markIdentityReady()` method + an explicit flush-on-ready) and still lets the caret set a local "Anonymous" state (just unbroadcast). The gate-Inner approach is a smaller, fully-contained diff, is zero-change in the common cached path, and eliminates the anonymous local state entirely. Trade-off accepted: in the rare pending-session window the collab editor shows the existing `Connecting…` placeholder a beat longer instead of rendering content with an anonymous caret. Since the same auth gate that lets a user reach the cockpit guarantees `me` resolves, the editor always mounts.

The R2-1 `updateUser` effect ([CollabEditor.tsx:68-70](../../../frontend/src/collab/CollabEditor.tsx#L68)) is **kept** as a cheap safety net for a mid-session profile-name change (harmless no-op when the name is unchanged); it is no longer the primary correctness mechanism.

### Files

- Modify: `frontend/src/collab/CollabEditor.tsx` — move `useSession` to the outer component; add `!me?.user` to the placeholder gate; thread `me` prop into `CollabEditorInner`; `Inner` reads `me` from props instead of calling `useSession`.
- Modify (test premise flip): `frontend/src/collab/CollabEditor.identity.test.tsx`.

### Test change

Rewrite the identity test's contract to the new behaviour (same harness, controlling when `sessionData.value` resolves):

- **Pre-resolve** (`sessionData.value === undefined`): the editor/caret does **not** mount → the provider's local awareness state carries **no `user`** (i.e. `stashed.awareness.getLocalState()?.user` is `undefined`), and the placeholder `data-testid="collab-connecting"` is shown. The old `toBe("Anonymous")` assertion is replaced by this "nothing anonymous broadcast" assertion.
- **Post-resolve** (set `sessionData.value = { user: { id: 7, name: "Late Name", … } }`, rerender): the editor mounts and the local awareness `user.name` is `"Late Name"` — and was **never** `"Anonymous"` at any point.

## 4. Fix #2 — declared-attribution griefing guard (backend)

### Problem

In `Room.handle`, awareness ids are attributed to the sending socket as `declared ∪ (states-diff)` ([room.py:217](../../../src/call_manager/proposal_assist/collab/room.py#L217)). Folding in the client-`declared` ids is deliberate (it makes a same-clientID reconnect robust when the states-diff is empty), but it lets a malicious member hand-craft an awareness frame that *declares a live peer's* client id. That id is then recorded in the attacker's `ClientState.awareness_ids`, so when the attacker disconnects, `remove_client_presence` scrubs the **victim's** caret for everyone (until the victim's next awareness re-announce). The states-diff term is already safe against this (a live peer's id is present in `before`, so it never appears in the diff); only the `declared` union is the vector.

### Mechanism — never attribute an id a live peer already owns

Before folding new ids into the sender's `awareness_ids`, subtract the set of ids currently owned by another live socket belonging to a **different user**:

```python
# Attribute ids to THIS socket: declared ids (robust to a same-clientID reconnect
# where the states-diff is empty) ∪ states-diff, MINUS any id owned by another live
# socket of a DIFFERENT user. That closes the griefing vector — a member declaring
# a *peer's* client id so its own disconnect scrubs the peer's caret — while leaving
# same-user re-attribution untouched, so a same-clientID reconnect that transiently
# overlaps its old socket (same user_id) still re-claims its id (R2-2 robustness).
owned_by_other_users: set[int] = set()
for client, st in self.clients.items():
    if client is not sender and st.user_id != state.user_id:
        owned_by_other_users |= st.awareness_ids
new_ids = (declared | (set(self.awareness.states) - before)) - owned_by_other_users
state.awareness_ids |= new_ids
```

`sender` is already a keyword arg of `handle`, and `self.clients` maps socket → `ClientState` ([room.py:110-116,72](../../../src/call_manager/proposal_assist/collab/room.py#L110)). Restricting the exclusion to *different-user* owners is the key: griefing and a same-clientID reconnect are indistinguishable from the frame alone, but a griefer is a different member and a reconnect is the same user. Subtracting from the **whole** attribution (not just `declared`) is defensive — the states-diff never contains a live peer's id anyway (it is already in `before`). Same-user "self-griefing" (a member scrubbing their own other-tab caret) is harmless and therefore intentionally allowed.

**Scope of the fix (be precise):** this closes the harmful part — an attacker's *disconnect* removing a victim's caret. It does **not** stop the attacker from *relaying* a spoofed state for the peer's id (the raw payload is still broadcast at [room.py:219](../../../src/call_manager/proposal_assist/collab/room.py#L219)); presence is display-only and already spoofable (a member can forge their own name/cursor), and a transient position-spoof self-heals via the victim's own awareness re-announce. Filtering/rewriting relayed payloads per-id would be invasive and is out of scope. Update the existing KNOWN-LIMITATION comment block ([room.py:210-216](../../../src/call_manager/proposal_assist/collab/room.py#L210)) to reflect that the disconnect-removal vector is now closed and only the (accepted, self-healing) position-spoof remains.

### Files

- Modify: `src/call_manager/proposal_assist/collab/room.py` — the attribution line + comment in `Room.handle`'s awareness branch.
- Test: `tests/proposal_assist/collab/test_collab_awareness.py` (extend the existing presence scaffold — the module that already drives `Room.handle` with fake client sockets; do NOT create a parallel presence-test module).

### Tests

Drive `Room.handle` directly with fake client sockets, reusing the existing presence-test scaffolding (`FakeClient`, `_awareness_frame`, `_noop_persist`, `ClientState`):

1. **Cross-user griefing blocked (new test):** victim V (`user_id=1`) announces its presence via `_awareness_frame` → id `X` attributed to V. Attacker A (`user_id=2`) *replays V's frame* (declaring V's live id `X`). Assert `X ∉ stateA.awareness_ids` (A does not capture a different user's id), and that after `room.remove_client_presence(stateA)`, `X` is still in `room.awareness.states` (V's caret survives A's disconnect).
2. **Fresh self-attribution intact (existing coverage):** the existing `test_awareness_relayed_to_not_persisted_not_to_sender` already asserts a sender captures its own id with a peer present — with the guard, `owned_by_other_users` is empty (the peer owns nothing), so it still passes. No new test needed; the plan runs it to confirm no regression.
3. **Same-user reconnect overlap intact (existing coverage, must stay green):** the existing `test_same_clientid_reconnect_is_attributed_via_payload_parse` keeps the old socket in `room.clients` (same `user_id=1`) when the reconnecting socket claims the same id. Because the guard only excludes *different-user* owners, the same-user reconnect still re-claims its id → this test passes **unchanged**. The plan explicitly re-runs it as the regression guard for the guard's scope.

## 5. Fix #4 — malformed-awareness JS-side test (frontend, test-only)

### Problem

The provider's inbound `ws.onmessage` already wraps the entire decode + `applyAwarenessUpdate` in a `try/catch` that drops malformed frames ([collabProvider.ts:159-181](../../../frontend/src/collab/collabProvider.ts#L159)), mirroring the server's `room.py` guard. The hardening exists but is **uncovered** — no test proves a malformed awareness frame is dropped without crashing the handler or corrupting awareness. This slice adds that test only; no code change.

### Files

- Modify (add test): `frontend/src/collab/collabProvider.test.ts`.

### Test

Using the existing `FakeSocket`/`makeProvider` harness, deliver a structurally-valid `MESSAGE_AWARENESS` frame whose inner payload has an **invalid-JSON state string** (deterministic throw inside `applyAwarenessUpdate`'s `JSON.parse`, version-independent):

```ts
function malformedAwarenessFrame(): Uint8Array {
  const inner = encoding.createEncoder();
  encoding.writeVarUint(inner, 1);            // 1 updated client
  encoding.writeVarUint(inner, 999);          // clientID
  encoding.writeVarUint(inner, 0);            // clock
  encoding.writeVarString(inner, "{not valid json");  // state → JSON.parse throws inside applyAwarenessUpdate
  const e = encoding.createEncoder();
  encoding.writeVarUint(e, MESSAGE_AWARENESS);
  encoding.writeVarUint8Array(e, encoding.toUint8Array(inner));
  return encoding.toUint8Array(e);
}

it("drops a malformed AWARENESS frame without crashing or corrupting awareness", () => {
  const { p, sockets } = makeProvider(true);
  const s = sockets[0]; s.open(); s.sent.length = 0;
  expect(() => s.deliver(malformedAwarenessFrame())).not.toThrow();   // handler survives
  expect(p.status).toBe("connected");                                 // no teardown
  expect([...p.awareness.getStates().values()].some((st) => (st as any).user)).toBe(false); // no bogus peer
  // provider still works afterwards: a subsequent VALID awareness frame applies
  s.deliver(awarenessFrameFor({ name: "Bob" }));
  const users = [...p.awareness.getStates().values()].map((st) => (st as any).user?.name);
  expect(users).toContain("Bob");
});
```

## 6. Item #3 — reconnect clock-collision (documented, accepted)

**What it is.** When a socket drops, the server's `remove_client_presence` calls y-awareness `remove_awareness_states`. For a *remote* id (the departed peer — not the server's own client id) this deletes the state and encodes the removal as `state: null` at the **same** clock; the clock is incremented only when a client removes its *own* id (the `clientID === awareness.clientID` branch in y-protocols `awareness.js`). Peers therefore hold that id at that clock with no state. If the *same* client reconnects with the *same* `doc.clientID`, its on-open flush re-announces at that **same** clock with a non-null state, which `applyAwarenessUpdate` does **not** apply — a non-null state cannot override an equal-clock entry (only a `null` state overrides at an equal clock). So the reconnected client's caret stays invisible to peers until its awareness renews at a **higher** clock.

**Why we accept it.** This is inherent to y-awareness's clock semantics combined with any server-side presence removal; standard `y-websocket` deployments exhibit the same dynamic. It **self-heals** on the client's next awareness renewal (which sets local state at a higher clock): any cursor/selection change bumps the clock immediately via `setLocalState`, and the y-protocols `Awareness` object created at [collabProvider.ts:50](../../../frontend/src/collab/collabProvider.ts#L50) runs a built-in renew interval that re-announces the local state once it is older than `outdatedTimeout / 2` (~15 s). So an *active* reconnected editor's caret reappears at once; only an *idle* reconnected client's caret lags until the next renew (≤ ~15 s). Impact is cosmetic and transient.

**Why we do not fix it.** The two candidate fixes are both worse than the symptom: (a) resetting the client's `clientID` on reconnect would orphan its in-flight doc edits and defeat CRDT convergence; (b) forcing an eager local re-announce (or shortening the interval) adds constant background awareness traffic for a rare edge. If field data after the flip shows idle-reconnect caret gaps are a real annoyance, an eager on-reconnect re-announce can be revisited in a later slice. This item is recorded as an accepted limitation in the design and mirrored as a short code comment near `remove_client_presence` in `room.py`.

## 7. Testing & Gates

- Backend: `EMBEDDING_MODEL=bge-m3 pytest -p no:cacheprovider tests/proposal_assist/collab/ tests/test_api_spec.py -q` (grouped-dir; the presence branch of `room.py` is exercised by `tests/proposal_assist/collab/`). `ruff check .` clean.
- Frontend: `pnpm test` (vitest) + `pnpm tsc` clean. New/changed: `CollabEditor.identity.test.tsx` (premise flip), `collabProvider.test.ts` (malformed-frame drop).
- **Flag-off parity gate (HARD):** confirm no HTTP endpoint, kernel, or doc-write path changed; the only backend change is the ungated presence-attribution line, and the only frontend change is inside the collab-editor render path (which does not mount when the flag is off). Independently reproduce the full grouped-dir backend run + frontend `tsc`/vitest before declaring merge-ready.

## 8. Risks & Rejected Alternatives

- **#1 delays content in the rare pending-session window.** Accepted: negligible because `me` is app-shell-cached before the cockpit renders; the auth gate guarantees `me` resolves. Rejected alternative: provider `identityReady` flag (more surface, still sets an anonymous local state).
- **#2 could over-restrict attribution.** Guarded against by scoping the exclusion to *different-user* live owners: a same-clientID reconnect is the same `user_id`, so it is never blocked — even during a transient old/new socket overlap — preserving R2-2. The threat model is a member scrubbing *another* member's caret; same-user "self-griefing" is harmless and allowed. Rejected alternatives: excluding *all* other live owners (breaks the same-user reconnect-overlap the `declared` union exists for — caught while grounding the plan against the existing reconnect test); rewriting/filtering relayed awareness payloads per-id (invasive; the residual position-spoof self-heals).
- **#4 is test-only.** No behaviour change; the risk is a flaky or non-deterministic assertion — mitigated by using an invalid-JSON state (deterministic `JSON.parse` throw) rather than a truncated buffer (version-dependent).

## 9. Deliverables Summary

| Item | Kind | Files |
|------|------|-------|
| #1 Anonymous flash | frontend fix + test | `CollabEditor.tsx`, `CollabEditor.identity.test.tsx` |
| #2 Griefing guard | backend fix + tests | `room.py`, `tests/proposal_assist/collab/test_collab_awareness.py` |
| #3 Clock-collision | documentation | this spec + `room.py` comment |
| #4 Malformed frame | frontend test-only | `collabProvider.test.ts` |
