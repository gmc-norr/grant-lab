# Collab Slice 3b-5b — Presence Follow-ups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three 3b-4 presence robustness follow-ups (Anonymous-caret flash, cross-user declared-attribution griefing, malformed-awareness JS test gap) and document the reconnect clock-collision as an accepted limitation.

**Architecture:** Three small, isolated changes plus one code comment. Backend: a cross-user exclusion in the ungated awareness-attribution line of `Room.handle`, and a documenting comment on `remove_client_presence`. Frontend: gate the collab editor's mount on a resolved session identity so the caret never seeds/broadcasts a fallback "Anonymous"; add a regression test for the already-present malformed-awareness drop guard. No schema, no migration, no endpoint change; the feature flag stays OFF.

**Tech Stack:** Python 3.12 / `uv` / `ruff` / `pytest` (+ pycrdt, testcontainers). Frontend: React 19 + Vite + Tiptap v3 + y-protocols + vitest + `pnpm`.

Design spec: `docs/superpowers/specs/2026-07-05-collab-slice3b5b-presence-followups-design.md`.

## Global Constraints

- Feature flag STAYS OFF; add no `enable_collab` source caller. Flag-off behaviour is byte-for-byte unchanged (the backend change is on the ungated presence path; the frontend change is inside the collab-editor render path, which does not mount when the flag is off).
- **No migration, no schema change.** Presence is ephemeral and never persisted.
- Backend changes are additive and confined to the presence path in `src/call_manager/proposal_assist/collab/room.py` — no change to the doc write/persist path, the kernel, or any HTTP endpoint.
- Run backend tests grouped-dir with `EMBEDDING_MODEL=bge-m3` and `-p no:cacheprovider`; ALWAYS include `tests/test_api_spec.py` in the final verify. Never run the whole suite (testcontainers PG drops under load).
- CI gate is `ruff check .` (not `ruff format`). Frontend gate is `tsc` + `vitest` (no separate TS linter).
- Explicit-path `git add` only (never `-A`). Build detached in a worktree under `.worktrees/`, isolated Python 3.12 venv, node_modules symlinked from the main tree. Stop before merge. Merge is LOCAL, not pushed, only on explicit ask.
- Frontend fresh `pnpm add` needs `--config.minimumReleaseAge=0` — but this slice adds NO dependencies.

---

## File Structure

- `src/call_manager/proposal_assist/collab/room.py` — MODIFY: cross-user exclusion in the awareness branch of `Room.handle` (~line 217); documenting comment in `remove_client_presence` (~line 235). Responsibility: the room's presence relay + attribution.
- `tests/proposal_assist/collab/test_collab_awareness.py` — MODIFY: add one griefing-guard test; existing reconnect/attribution tests are the regression guard. Responsibility: presence-relay unit coverage.
- `frontend/src/collab/CollabEditor.tsx` — MODIFY: move `useSession` to the outer component, gate the mount on `me?.user`, thread `me` into `CollabEditorInner`. Responsibility: mounts the Tiptap collab editor + caret.
- `frontend/src/collab/CollabEditor.identity.test.tsx` — MODIFY: flip the test premise to "no Anonymous broadcast until identity resolves". Responsibility: identity-seeding behaviour.
- `frontend/src/collab/CollabEditor.test.tsx` — MODIFY: add a resolved-session mock so the mount gate is satisfied. Responsibility: read-only + StrictMode render coverage.
- `frontend/src/collab/collabProvider.test.ts` — MODIFY: add a malformed-awareness-frame drop regression test. Responsibility: provider wire-protocol coverage.

---

## Task 1: Backend — cross-user griefing guard (#2) + documented reconnect-clock limitation (#3)

**Files:**
- Modify: `src/call_manager/proposal_assist/collab/room.py` (awareness branch of `Room.handle`, ~line 208-217; `remove_client_presence`, ~line 235)
- Test: `tests/proposal_assist/collab/test_collab_awareness.py`

**Interfaces:**
- Consumes: `Room.handle(*, sender, state: ClientState, message: bytes, persist)`, `ClientState(user_id: int, can_write: bool, awareness_ids: set[int])`, `room.clients: dict[socket, ClientState]`, `room.awareness` (pycrdt `Awareness`), `room.remove_client_presence(state)`. Test helpers already in the module: `FakeClient`, `_awareness_frame(user) -> (frame, client_id)`, `_noop_persist`.
- Produces: no new public symbols. Behaviour change: an awareness id owned by another live socket of a **different** `user_id` is excluded from the sender socket's attributed `awareness_ids`.

- [ ] **Step 1: Write the failing test**

Add to `tests/proposal_assist/collab/test_collab_awareness.py`:

```python
@pytest.mark.anyio
async def test_declaring_a_different_users_live_id_does_not_capture_it():
    # Griefing guard (Slice 3b-5b #2): a member must not capture an awareness id a
    # LIVE peer of a DIFFERENT user already owns — otherwise the attacker's
    # disconnect would scrub the victim's caret for everyone.
    room = Room((uuid.uuid4(), "draft"))
    victim, attacker = FakeClient(), FakeClient()
    st_victim = ClientState(user_id=1, can_write=True)
    st_attacker = ClientState(user_id=2, can_write=True)   # DIFFERENT user
    room.clients[victim] = st_victim
    room.clients[attacker] = st_attacker
    frame, cid = _awareness_frame({"user": {"name": "Victim"}})

    await room.handle(sender=victim, state=st_victim, message=frame, persist=_noop_persist)
    assert cid in st_victim.awareness_ids                  # victim owns its id

    # attacker (different user) REPLAYS the victim's frame, declaring the victim's id
    await room.handle(sender=attacker, state=st_attacker, message=frame, persist=_noop_persist)
    assert cid not in st_attacker.awareness_ids            # guard: not captured

    # the victim's caret survives the attacker's disconnect
    await room.remove_client_presence(st_attacker)
    assert cid in room.awareness.states
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `EMBEDDING_MODEL=bge-m3 pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_awareness.py::test_declaring_a_different_users_live_id_does_not_capture_it -q`
Expected: FAIL — current code does `state.awareness_ids |= declared | ...`, so the attacker captures `cid` (`assert cid not in st_attacker.awareness_ids` fails), and `remove_client_presence` then scrubs `cid` (`assert cid in room.awareness.states` fails).

- [ ] **Step 3: Implement the cross-user guard**

In `src/call_manager/proposal_assist/collab/room.py`, replace the attribution comment+line in the awareness branch of `Room.handle` (currently):

```python
                # Attribute ids to THIS socket: payload-declared ids (robust to a
                # same-clientID reconnect where the diff is empty) ∪ states-diff.
                # KNOWN LIMITATION: folding in `declared` means a MALICIOUS member
                # could hand-craft a frame declaring a *peer's* client id, so that
                # this socket's disconnect removes the peer's caret for everyone.
                # It is transient (the victim's ~15s awareness heartbeat re-adds it)
                # and presence is display-only/spoofable by design (a member can
                # already forge their own name/cursor), so we accept it rather than
                # give up the reconnect-robustness the `declared` union buys.
                state.awareness_ids |= declared | (set(self.awareness.states) - before)
```

with:

```python
                # Attribute ids to THIS socket: payload-declared ids (robust to a
                # same-clientID reconnect where the states-diff is empty) ∪ states-diff,
                # MINUS any id owned by another live socket of a DIFFERENT user. That
                # closes the griefing vector — a member declaring a *peer's* client id
                # so its own disconnect scrubs the peer's caret — while leaving same-user
                # re-attribution untouched: a same-clientID reconnect that transiently
                # overlaps its old socket (same user_id) still re-claims its id (R2-2).
                # Griefing vs reconnect is indistinguishable from the frame alone, but a
                # griefer is a different member and a reconnect is the same user. The
                # residual (accepted) exposure is a transient position-spoof: the raw
                # payload is still relayed below, but presence is display-only/spoofable
                # by design and self-heals via the victim's ~15s awareness re-announce.
                owned_by_other_users: set[int] = set()
                for client, st in self.clients.items():
                    if client is not sender and st.user_id != state.user_id:
                        owned_by_other_users |= st.awareness_ids
                new_ids = (declared | (set(self.awareness.states) - before)) - owned_by_other_users
                state.awareness_ids |= new_ids
```

- [ ] **Step 4: Run the new test + the existing attribution/reconnect regression tests**

Run: `EMBEDDING_MODEL=bge-m3 pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_awareness.py -q`
Expected: PASS — the new test passes, and crucially the existing `test_awareness_relayed_to_others_not_persisted_not_to_sender` (fresh self-attribution with a peer present) and `test_same_clientid_reconnect_is_attributed_via_payload_parse` (same-`user_id` old/new socket overlap) still pass unchanged, confirming the guard's cross-user scope did not regress legitimate attribution.

- [ ] **Step 5: Add the #3 documented-limitation comment**

In `remove_client_presence`, insert the note between the docstring and `ids = list(...)`:

```python
    async def remove_client_presence(self, state: ClientState) -> None:
        """Remove a departed client's awareness ids and broadcast the removal to
        remaining clients so their carets drop immediately (not after the ~30s
        timeout). No-op if the client owned no presence. Callers invoke this only
        when the room still has peers (see RoomManager.release/drop_client)."""
        # ACCEPTED LIMITATION (reconnect clock-collision, Slice 3b-5b #3): removing a
        # REMOTE id here encodes the removal as state=null at the id's CURRENT clock
        # (y-awareness bumps the clock only when a client removes its OWN id). If the
        # same client reconnects with the same doc.clientID, its on-open flush
        # re-announces at that same clock and applyAwarenessUpdate ignores it (a
        # non-null state cannot override an equal-clock entry), so the reconnected
        # caret is briefly invisible to peers. It self-heals at the next awareness
        # renewal: immediately on cursor activity, or <= ~15s via the y-protocols
        # renew interval (outdatedTimeout/2). Inherent to y-awareness + server-side
        # removal; standard y-websocket shares it. Documented, not fixed.
        ids = list(state.awareness_ids)
        if not ids:
            return
```

- [ ] **Step 6: Lint + re-run the file**

Run: `ruff check src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_awareness.py`
Then: `EMBEDDING_MODEL=bge-m3 pytest -p no:cacheprovider tests/proposal_assist/collab/test_collab_awareness.py -q`
Expected: ruff clean; all tests pass.

- [ ] **Step 7: Commit**

```bash
git add src/call_manager/proposal_assist/collab/room.py tests/proposal_assist/collab/test_collab_awareness.py
git commit -m "feat(collab): cross-user griefing guard + document reconnect-clock limit (3b-5b #2,#3)"
```

---

## Task 2: Frontend — gate collab editor mount on resolved identity (#1)

**Files:**
- Modify: `frontend/src/collab/CollabEditor.tsx`
- Test: `frontend/src/collab/CollabEditor.identity.test.tsx` (premise flip)
- Test fixture: `frontend/src/collab/CollabEditor.test.tsx` (add resolved-session mock)

**Interfaces:**
- Consumes: `useSession()` (returns `{ data }` where `data` is the `/api/me` response or `undefined`), `displayName(user?)`, `colorForUserId(id)`, `useCollabDoc(props) -> { doc, provider, status }`.
- Produces: `CollabEditorInner` now takes an added prop `me: NonNullable<ReturnType<typeof useSession>["data"]>` (guaranteed non-null by the outer gate). External surface of `CollabEditor` is unchanged.

- [ ] **Step 1: Rewrite the identity test to the new contract (failing test)**

Replace the body of `frontend/src/collab/CollabEditor.identity.test.tsx`'s `describe` with:

```tsx
describe("CollabEditor identity (Slice 3b-5b #1: no Anonymous flash)", () => {
  it("does not broadcast any identity until the session resolves, then mounts with the real name", async () => {
    stashed = null;
    sessionData.value = undefined; // unresolved at mount
    const { rerender } = renderWithProviders(
      <CollabEditor workspaceId="w" artifact="draft" canWrite />,
    );
    await waitFor(() => expect(stashed).not.toBeNull());
    // pre-resolve: the editor/caret is NOT mounted, so no local awareness user is
    // ever set — nothing "Anonymous" is broadcast.
    expect((stashed!.awareness.getLocalState() as any)?.user).toBeUndefined();

    sessionData.value = { user: { id: 7, name: "Late Name", email: "late@ki.se" } };
    rerender(<CollabEditor workspaceId="w" artifact="draft" canWrite />);
    await waitFor(() => {
      expect((stashed!.awareness.getLocalState() as any)?.user?.name).toBe("Late Name");
    });
    // and it was NEVER the "Anonymous" fallback
    expect((stashed!.awareness.getLocalState() as any)?.user?.name).not.toBe("Anonymous");
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run (cwd `frontend`): `pnpm exec vitest run src/collab/CollabEditor.identity.test.tsx`
Expected: FAIL — current code mounts `CollabEditorInner` (which calls `useSession` internally) as soon as `doc`/`provider` exist, so the caret seeds the provider's local awareness with `{ user: { name: "Anonymous" } }` pre-resolve; `expect(...user).toBeUndefined()` fails.

- [ ] **Step 3: Implement the identity mount-gate**

In `frontend/src/collab/CollabEditor.tsx`, change the outer `CollabEditor` to fetch the session and gate on it, passing `me` down:

```tsx
export function CollabEditor(props: CollabEditorProps) {
  const { doc, provider, status } = useCollabDoc(props);
  const { data: me } = useSession();
  // Clear the shared editor store on unmount so a stale collab save-status does not
  // leak into the next cockpit tab/view — mirrors the CM6 ArtifactSectionEditor.
  useEffect(() => () => resetEditorStore(), []);
  // Gate the editor mount on a RESOLVED identity as well as the live doc/provider.
  // Mounting CollaborationCaret before useSession() resolves would seed and
  // broadcast an "Anonymous" fallback caret for one render (Slice 3b-5b #1). The
  // doc/provider are still built + connected above, so sync is not delayed; only the
  // editable surface waits for identity — normally instant (me is app-shell cached),
  // and the cockpit's auth gate guarantees me resolves for anyone who reaches it.
  if (!doc || !provider || !me?.user) {
    return (
      <div data-collab-status={status} data-testid="collab-connecting">
        Connecting…
      </div>
    );
  }
  return (
    <CollabEditorInner {...props} doc={doc} provider={provider} status={status} me={me} />
  );
}
```

Then change `CollabEditorInner` to read identity from the prop (no internal `useSession`):

```tsx
function CollabEditorInner(
  props: CollabEditorProps & {
    doc: Y.Doc;
    provider: CollabProvider;
    status: ProviderStatus;
    me: NonNullable<ReturnType<typeof useSession>["data"]>;
  },
) {
  const label = displayName(props.me.user);
  const color = colorForUserId(props.me.user?.id ?? 0);
  const editor = useEditor(
    {
      editable: props.canWrite,
      extensions: [
        ...collabEditorExtensions,
        Collaboration.configure({ document: props.doc, field: "prosemirror" }),
        // Identity is resolved before this component mounts (gated in CollabEditor),
        // so the caret seeds with the real name/color — never a fallback "Anonymous".
        CollaborationCaret.configure({ provider: props.provider, user: { name: label, color } }),
      ],
    },
    [props.doc], // editor keyed ONLY on the doc — NOT rebuilt on identity change
  );

  // Keep identity fresh if the user renames themselves mid-session (a cheap no-op
  // otherwise). The mount gate — not this effect — is now the primary mechanism.
  useEffect(() => {
    if (editor) editor.commands.updateUser({ name: label, color });
  }, [editor, label, color]);

  return (
    <div data-collab-status={props.status}>
      <SaveVersionButton
        id={props.workspaceId}
        artifact={props.artifact}
        provider={props.provider}
        canWrite={props.canWrite}
        status={props.status}
      />
      <EditorContent editor={editor} data-testid="collab-editor" />
      {props.artifact === "draft" && props.sections != null && props.head != null && editor && (
        <TiptapSelectionToolbar
          editor={editor}
          provider={props.provider}
          workspaceId={props.workspaceId}
          head={props.head}
          sections={props.sections}
          canEdit={props.canEdit ?? false}
          canWrite={props.canWrite}
        />
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run the identity test to verify it passes**

Run (cwd `frontend`): `pnpm exec vitest run src/collab/CollabEditor.identity.test.tsx`
Expected: PASS — pre-resolve the gate returns the placeholder (no caret, no local awareness user); post-resolve the editor mounts with `"Late Name"`.

- [ ] **Step 5: Fix the sibling render test's fixture (required by the gate)**

`CollabEditor.test.tsx` renders the real `CollabEditor` without a session mock, so the new gate would leave it on the `Connecting…` placeholder and its `findByTestId("collab-editor")` would time out. Add a resolved-session mock near the top of `frontend/src/collab/CollabEditor.test.tsx` (below the existing `vi.mock` calls, before `import { CollabEditor }`):

```tsx
vi.mock("../api/session", () => ({
  useSession: () => ({ data: { user: { id: 1, name: "Tester", email: "t@ki.se" } } }),
}));
```

- [ ] **Step 6: Run both collab-editor render tests**

Run (cwd `frontend`): `pnpm exec vitest run src/collab/CollabEditor.test.tsx src/collab/CollabEditor.identity.test.tsx`
Expected: PASS — both `CollabEditor.test.tsx` tests find `collab-editor` (session now resolved) and the identity test passes.

- [ ] **Step 7: Type-check**

Run (cwd `frontend`): `pnpm exec tsc --noEmit`
Expected: no errors. (The outer gate narrows `me` to non-undefined after `!me?.user`, so `me={me}` satisfies the `NonNullable<...>` prop type.)

- [ ] **Step 8: Commit**

```bash
git add frontend/src/collab/CollabEditor.tsx frontend/src/collab/CollabEditor.identity.test.tsx frontend/src/collab/CollabEditor.test.tsx
git commit -m "feat(collab): gate collab editor mount on resolved identity — no Anonymous flash (3b-5b #1)"
```

---

## Task 3: Frontend — malformed-awareness-frame drop regression test (#4)

**Files:**
- Test: `frontend/src/collab/collabProvider.test.ts`

**Interfaces:**
- Consumes: existing test harness in the file — `makeProvider(canWrite)`, `FakeSocket` (`.open()`, `.deliver(bytes)`), `awarenessFrameFor(user)`, and the `MESSAGE_AWARENESS` constant; `encoding` from `lib0/encoding`. No production code changes — this locks the existing drop guard at `collabProvider.ts` `ws.onmessage` (the `try { ... } catch { /* drop malformed frame */ }`).
- Produces: no new symbols.

- [ ] **Step 1: Write the regression test**

Add to `frontend/src/collab/collabProvider.test.ts` (inside the `describe("CollabProvider", ...)` block):

```ts
function malformedAwarenessFrame(): Uint8Array {
  // A structurally-valid AWARENESS frame whose inner payload carries an
  // invalid-JSON state string → applyAwarenessUpdate's JSON.parse throws
  // (deterministic + version-independent), exercising the provider's drop guard.
  const inner = encoding.createEncoder();
  encoding.writeVarUint(inner, 1); // one updated client
  encoding.writeVarUint(inner, 999); // clientID
  encoding.writeVarUint(inner, 0); // clock
  encoding.writeVarString(inner, "{not valid json"); // state → JSON.parse throws
  const e = encoding.createEncoder();
  encoding.writeVarUint(e, MESSAGE_AWARENESS);
  encoding.writeVarUint8Array(e, encoding.toUint8Array(inner));
  return encoding.toUint8Array(e);
}

it("drops a malformed AWARENESS frame without crashing or corrupting awareness", () => {
  const { p, sockets } = makeProvider(true);
  const s = sockets[0];
  s.open();
  s.sent.length = 0;
  expect(() => s.deliver(malformedAwarenessFrame())).not.toThrow(); // handler survives
  expect(p.status).toBe("connected"); // no teardown
  expect([...p.awareness.getStates().values()].some((st) => (st as any).user)).toBe(false); // no bogus peer
  // provider still works afterwards: a subsequent VALID awareness frame applies
  s.deliver(awarenessFrameFor({ name: "Bob" }));
  const users = [...p.awareness.getStates().values()].map((st) => (st as any).user?.name);
  expect(users).toContain("Bob");
});
```

- [ ] **Step 2: Run the test to verify it passes**

Run (cwd `frontend`): `pnpm exec vitest run src/collab/collabProvider.test.ts`
Expected: PASS on first run — the drop guard already exists in `collabProvider.ts`; this test locks it. (Sanity: were the `try/catch` around `applyAwarenessUpdate` removed, `s.deliver(...)` would throw and this test would fail — that is what it protects.)

- [ ] **Step 3: Type-check**

Run (cwd `frontend`): `pnpm exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/collab/collabProvider.test.ts
git commit -m "test(collab): lock malformed-awareness-frame drop guard (3b-5b #4)"
```

---

## Final Verification (before stop-before-merge)

- [ ] **Backend grouped-dir run including the API-spec guard:**
  Run: `EMBEDDING_MODEL=bge-m3 pytest -p no:cacheprovider tests/proposal_assist/collab/ tests/test_api_spec.py -q`
  Expected: all pass. (`test_api_spec.py` guards against unintended OpenAPI/schema drift — this slice must not change it.)

- [ ] **Backend lint:** `ruff check .` — clean.

- [ ] **Frontend full suite (NOT just the collab files):**
  Run (cwd `frontend`): `pnpm exec vitest run`
  Expected: all files pass. Running the whole suite catches any other test that renders the real `CollabEditor` without a resolved session (grep first: `grep -rn "CollabEditor" frontend/src --include=*.tsx --include=*.ts -l` — expect only `CollabEditor.tsx`, `CollabEditor.test.tsx`, `CollabEditor.identity.test.tsx`, and `DraftPanel*` which stubs it).

- [ ] **Frontend type-check:** (cwd `frontend`) `pnpm exec tsc --noEmit` — clean.

- [ ] **Flag-off parity check (HARD gate):** confirm the diff touches only (a) the ungated awareness-attribution + `remove_client_presence` comment in `room.py`, and (b) the collab-editor render path in the frontend. No HTTP endpoint, kernel, doc-write path, schema, migration, or `enable_collab` caller changed. Run `git diff --stat master...HEAD` and eyeball.

- [ ] **STOP before merge.** Report the branch, commit list, and gate results for independent verification + adversarial review. Do not merge or push.

---

## Self-Review

- **Spec coverage:** #1 → Task 2; #2 → Task 1 (Steps 1-4, 6-7); #3 → Task 1 (Step 5); #4 → Task 3. All four spec items mapped.
- **Placeholders:** none — every code step carries complete code and exact commands.
- **Type/name consistency:** `owned_by_other_users`, `new_ids`, `ClientState.user_id`, `room.clients`, `remove_client_presence` match `room.py`; `me: NonNullable<ReturnType<typeof useSession>["data"]>`, `displayName`, `colorForUserId`, `useCollabDoc` match `CollabEditor.tsx`; `makeProvider`/`FakeSocket.deliver`/`awarenessFrameFor`/`MESSAGE_AWARENESS` match `collabProvider.test.ts`.
- **Regression guardrails:** Task 1 Step 4 pins the existing reconnect/attribution tests; Task 2 Step 5 fixes the sibling fixture the gate would otherwise break; Final Verification runs the FULL frontend suite, not just the changed files.
