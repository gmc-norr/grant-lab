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

Before folding new ids into the sender's `awareness_ids`, subtract the set of ids currently owned by **other live sockets**:

```python
# Attribute ids to THIS socket: declared ids (robust to a same-clientID reconnect
# where the states-diff is empty) ∪ states-diff, MINUS any id another live socket
# already owns. Excluding peer-owned ids closes the griefing vector where a member
# declares a live peer's client id so its own disconnect scrubs the peer's caret.
# A genuine same-clientID reconnect is unaffected: the prior socket's ids were
# removed from its ClientState on disconnect, so no LIVE client owns them.
owned_by_others: set[int] = set()
for client, st in self.clients.items():
    if client is not sender:
        owned_by_others |= st.awareness_ids
new_ids = (declared | (set(self.awareness.states) - before)) - owned_by_others
state.awareness_ids |= new_ids
```

`sender` is already a keyword arg of `handle`, and `self.clients` maps socket → `ClientState` ([room.py:110-116,72](../../../src/call_manager/proposal_assist/collab/room.py#L110)). Subtracting `owned_by_others` from the **whole** attribution (not just `declared`) is strictly safer and preserves reconnect-robustness because a departed socket is popped from `self.clients` before its successor connects.

**Scope of the fix (be precise):** this closes the harmful part — an attacker's *disconnect* removing a victim's caret. It does **not** stop the attacker from *relaying* a spoofed state for the peer's id (the raw payload is still broadcast at [room.py:219](../../../src/call_manager/proposal_assist/collab/room.py#L219)); presence is display-only and already spoofable (a member can forge their own name/cursor), and a transient position-spoof self-heals via the victim's own awareness re-announce. Filtering/rewriting relayed payloads per-id would be invasive and is out of scope. Update the existing KNOWN-LIMITATION comment block ([room.py:210-216](../../../src/call_manager/proposal_assist/collab/room.py#L210)) to reflect that the disconnect-removal vector is now closed and only the (accepted, self-healing) position-spoof remains.

### Files

- Modify: `src/call_manager/proposal_assist/collab/room.py` — the attribution line + comment in `Room.handle`'s awareness branch.
- Test: `tests/proposal_assist/collab/test_room_presence.py` (extend the existing presence tests; create if absent — mirror the existing room-presence test module).

### Tests

Drive `Room.handle` directly with two fake client sockets (`sender` A and peer B), reusing the existing presence-test scaffolding:

1. **Griefing blocked:** B declares id `X` (B's own). A then sends an awareness frame *declaring* `X`. Assert `X ∉ clientA_state.awareness_ids` (A did not capture B's id), and that after A's `remove_client_presence`, `X` is still present in `room.awareness.states` (B's caret survives A's disconnect).
2. **Legitimate self-attribution intact:** a fresh socket C declaring its own id `Z` (owned by no live peer) → `Z ∈ clientC_state.awareness_ids`.
3. **Same-clientID reconnect intact:** socket D owns `W`; D disconnects (popped from `self.clients`, presence removed); a new socket D2 declares `W` → `W ∈ clientD2_state.awareness_ids` (no live owner remained, so the guard does not block re-claim).

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

**What it is.** When a socket drops, the server's `remove_client_presence` calls y-awareness `remove_awareness_states`, which **bumps the awareness clock** for the removed ids (so the removal propagates to peers). If the *same* client reconnects with the *same* `doc.clientID`, its local awareness clock resumes below the removal clock, so its first re-announce is treated as stale and ignored by peers — the reconnected client's caret is briefly invisible to others.

**Why we accept it.** This is inherent to y-awareness's clock semantics combined with any server-side presence removal; standard `y-websocket` deployments exhibit the same dynamic. It **self-heals**: the y-protocols `Awareness` object created at [collabProvider.ts:50](../../../frontend/src/collab/collabProvider.ts#L50) runs a built-in re-announce interval (`outdatedTime/2`, ~15 s) that bumps the local clock, and any cursor/selection activity bumps it immediately. So an *active* reconnected editor's caret reappears at once; only an *idle* reconnected client's caret may lag by one to two re-announce cycles (~15–30 s). Impact is cosmetic and transient.

**Why we do not fix it.** The two candidate fixes are both worse than the symptom: (a) resetting the client's `clientID` on reconnect would orphan its in-flight doc edits and defeat CRDT convergence; (b) forcing an eager local re-announce (or shortening the interval) adds constant background awareness traffic for a rare edge. If field data after the flip shows idle-reconnect caret gaps are a real annoyance, an eager on-reconnect re-announce can be revisited in a later slice. This item is recorded as an accepted limitation in the design and mirrored as a short code comment near `remove_client_presence` in `room.py`.

## 7. Testing & Gates

- Backend: `EMBEDDING_MODEL=bge-m3 pytest -p no:cacheprovider tests/proposal_assist/collab/ tests/test_api_spec.py -q` (grouped-dir; the presence branch of `room.py` is exercised by `tests/proposal_assist/collab/`). `ruff check .` clean.
- Frontend: `pnpm test` (vitest) + `pnpm tsc` clean. New/changed: `CollabEditor.identity.test.tsx` (premise flip), `collabProvider.test.ts` (malformed-frame drop).
- **Flag-off parity gate (HARD):** confirm no HTTP endpoint, kernel, or doc-write path changed; the only backend change is the ungated presence-attribution line, and the only frontend change is inside the collab-editor render path (which does not mount when the flag is off). Independently reproduce the full grouped-dir backend run + frontend `tsc`/vitest before declaring merge-ready.

## 8. Risks & Rejected Alternatives

- **#1 delays content in the rare pending-session window.** Accepted: negligible because `me` is app-shell-cached before the cockpit renders; the auth gate guarantees `me` resolves. Rejected alternative: provider `identityReady` flag (more surface, still sets an anonymous local state).
- **#2 could over-restrict attribution.** Guarded against: subtracting only *other live* sockets' ids; a departed socket's ids are already gone from `self.clients`, so same-clientID reconnect still re-claims. Rejected alternative: rewriting/filtering relayed awareness payloads per-id (invasive; the residual position-spoof self-heals).
- **#4 is test-only.** No behaviour change; the risk is a flaky or non-deterministic assertion — mitigated by using an invalid-JSON state (deterministic `JSON.parse` throw) rather than a truncated buffer (version-dependent).

## 9. Deliverables Summary

| Item | Kind | Files |
|------|------|-------|
| #1 Anonymous flash | frontend fix + test | `CollabEditor.tsx`, `CollabEditor.identity.test.tsx` |
| #2 Griefing guard | backend fix + tests | `room.py`, `tests/proposal_assist/collab/test_room_presence.py` |
| #3 Clock-collision | documentation | this spec + `room.py` comment |
| #4 Malformed frame | frontend test-only | `collabProvider.test.ts` |
