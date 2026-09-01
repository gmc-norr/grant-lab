# Branch-leak fix: the two bugs still open

**Date:** 2026-09-01
**Context:** follow-up to the cross-branch draft-leak fix. Two of the four defects
found in review are fixed and merged; two remain, each pinned by a test that
passes *because* the bug is present.

## What is already done

| commit | what it fixed |
|---|---|
| `e312b373` | a branch switch replaces the Y.Doc (close 4409) instead of merging into it |
| `183f8cdd` | a save that resolves after a branch switch no longer poisons the query cache |
| `ad2aaee1` | a save carries `base_branch`; the server refuses it if the workspace moved |
| `99067e1c` | closed one hollow gate; pinned the three defects the fix does not reach |

The user's live PathAssist workspace was corrupted by the original bug (3,183 →
41,539 bytes, 78 headings, 13 duplicate copies of six sections). It was restored
on 2026-09-01: git set to the clean text (`d7f294e`, both `main` and `test`), and
the authoritative 57KB Yjs log cleared with `store.truncate_log` +
`state.reset_checkpoint` so the next open rehydrates from git HEAD. Backups in
`~/pathassist-backup/` (full bundle, repo copy, corrupted file, Yjs blob,
manifest). **Do not delete that directory** until the user says so.

---

## Bug 2 — the 4409 reconnect storm has no cap and no backoff

**Severity:** high (a server stuck saying "replaced" spins the browser forever).
**Estimated work:** ~30 minutes. One file, one test.

### Where

`frontend/src/collab/useCollabDoc.ts` — the whole hook is ~72 lines; the relevant
part is the `documentGeneration` state at line 34 and the effect at lines 36-70.

```ts
const [documentGeneration, setDocumentGeneration] = useState(0);
...
onDocumentReplaced: () => {
  setState(null);
  setStatus("connecting");
  setSynced(false);
  setDocumentGeneration((generation) => generation + 1);
},
...
}, [workspaceId, artifact, documentGeneration]);
```

`documentGeneration` is an effect dependency, so every 4409 tears down the
provider and builds a **brand new** `CollabProvider` and `Y.Doc`. There is no
counter and no delay between rebuilds.

### The trap — this cost one wasted attempt already

**The bound MUST live in `useCollabDoc`, beside `documentGeneration`. It cannot
live in `CollabProvider`.**

A first attempt capped the rebuilds using `this.attempts` inside
`CollabProvider`, and the pinning test still passed. Root cause: every
replacement constructs a *new* provider instance, so any per-provider counter
resets to zero on each rebuild and can never reach a cap. Capping in the hook
instead was verified to turn the test red.

The same reasoning rules out routing it through `scheduleReconnect()` —
`collabProvider.ts:267-274` deliberately `return`s on the 4409 arm *before*
`scheduleReconnect()`, because a 4409 must not be retried on the old doc.

### The test that pins it

`frontend/src/collab/useCollabDoc.test.tsx:64`

```
it("KNOWN GAP: every replacement builds a new socket, with no cap and no backoff", ...)
```

It drives 20 replacement rounds and asserts 21 sockets were built. **This test
passing is the bug.** When the cap lands, invert it: assert the socket count
stops at the cap, and that a rebuild after the cap does not happen.

Do not convert it to `xfail`/`skip` — an `xfail` is satisfied by *any* failure,
so it cannot prove the specific bad thing stopped happening.

### Suggested shape

A rebuild counter and a small backoff, both reset when a connection actually
syncs (a successful sync means the storm ended). Pick a cap (8 was floated) and
surface a terminal state so the editor can tell the user to reload rather than
silently spinning.

---

## Bug 3 — a read can return the new branch's bytes under the old branch's name

**Severity:** high (it is the exact leak `branch` was added to prevent, in the
one ordering `branch` does not cover).
**Estimated work:** ~1 hour for option A, ~3-4 hours for option B. **Needs a
decision before implementation — see below.**

### Where

`src/call_manager/proposal_assist/branches.py`, `switch_branch()` (line 153).
It sets `workspaces.current_branch` (step 8) and then runs `checkout_branch`
(step 9) **inside** the transaction. The commit happens afterwards, in the route
(`switch_branch_route`).

So between step 9 and the commit, the git worktree already holds the **target**
branch's bytes while every other connection still reads the **source** branch's
name.

`get_draft` (`src/call_manager/web/api/workspaces.py:275-300`) reads exactly that
pair, and takes no mutex:

```python
ws = require_member(workspace_id=workspace_id, user=user, session=session)
stamp = stamp_for_workspace(ws)
branch = ws.current_branch                    # <- from ITS OWN session (stale)
...
repo_dir = workspace_repo_dir(settings.workspaces_dir, workspace_id)
proposal = repo_dir / PROPOSAL_FILENAME
head = current_head(repo_dir)
raw_disk = proposal.read_text()               # <- from the worktree (already new)
```

The client then sees `branch` unchanged, so the new `branchChanged` guard reads
false, the editor buffers and undo history survive, and the other branch's text
is rendered as if it belonged to this branch.

**Four read sites** in `web/api/workspaces.py` read `ws.current_branch` this way:
lines **281, 335, 511, 557**.

### The test that pins it

`tests/proposal_assist/test_api_branches.py:408`

```
def test_KNOWN_DEFECT_the_worktree_changes_before_the_branch_rename_commits(...)
```

It monkeypatches `branches_mod.checkout_branch` to peek at the seam with a fresh
session at the exact instant between step 9 and the commit, reading the same two
sources `get_draft` reads. (It peeks rather than racing a real second HTTP
request because nested TestClient portals deadlock.)

Its docstring already names the fix condition: *"Closing the window — committing
before the checkout, or making the read take the commit mutex — turns the
assertions below red."* **This test passing is the bug.** Invert it when fixed.

### The decision — two candidate fixes, and they are not equivalent

**Option A — derive the branch name from the worktree, not the DB column.**
Report the branch by reading the worktree's own HEAD (`git symbolic-ref`) in the
read paths, so the name and the bytes come from the same place and cannot
disagree. ~1 hour, four call sites.

- Pro: fixes the actual lie (name not matching bytes). No new locking, no new
  deadlock surface, no cost on the hottest read path.
- Con: bends spec §4.1, which makes the **database** the source of truth for
  which branch a workspace is on. Note the reads in question are reporting *what
  was just served*, not *deciding* anything — which is the argument for A.

**Option B — make the four reads take the commit mutex.**
Follows §4.1 literally. ~3-4 hours.

- Pro: spec-faithful; one mechanism for all readers.
- **Con — the real one:** these reads call `require_member` first, which loads
  the workspace row, and would then take the mutex. That is `workspaces` before
  `mutex`, which **inverts the project's documented lock order**
  (`mutex < jobs < workspaces < calls`). That is a deadlock hazard, not a style
  nit, and it must be resolved (hoist the mutex above `require_member`) rather
  than waved through. It also puts a lock on the hottest read: a reader can wait
  up to the 10s mutex timeout while a switch is in flight.

**Recommendation: A.** It removes the inconsistency an hour from now, adds no
deadlock risk, and does not slow every draft read. B is the change that costs a
day when the lock ordering goes wrong.

If B is chosen anyway, the lock must be acquired **before** `require_member`, and
the change needs its own test proving the order (the project already has a memory
noting that "lock order has NO test").

---

## Ground rules for whoever picks this up

- **Prove each fix by breaking it.** A green suite is not evidence a new gate
  bites. For each of the two, mutate the fix and confirm the *specific* inverted
  test goes red for the *right reason*. Mutate **by line number** — a previous
  session patched the wrong one of two identical lines and drew a false
  conclusion.
- **The `KNOWN` tests are inverted, not deleted.** Each one currently passes
  *because* the defect exists; after the fix it must assert the defect is gone.
- **Test suite:** `uv run pytest -n 16 --dist loadfile` (~1m). Expect
  `3361 passed, 1 skipped` **plus** the tests added since; read `1 skipped` as
  the signal, not the total. Frontend: `pnpm test`, `pnpm exec tsc --noEmit`.
- **CI runs `ruff check` only** — *not* `ruff format --check`
  (`.github/workflows/ci.yml:44`). Do not reformat unrelated lines.
- Regenerate `docs/ARCHITECTURE.md` in the same commit if structure moves:
  `uv run python scripts/gen_architecture.py`.
- Untracked in the tree right now and **not** part of this work: `.vite/`,
  `*.orig` files, and another session's
  `docs/superpowers/reviews/2026-09-01-whole-project-review.md`. Do not `git add -A`.
