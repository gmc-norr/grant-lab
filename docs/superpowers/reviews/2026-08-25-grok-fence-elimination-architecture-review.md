# Grok review: eliminate the lifecycle fence

**Date:** 2026-08-25
**Reviewer:** Grok 4.6 (GitHub Copilot), second opinion on `/home/parlar_ai/fence-problem-brief.md`
**Ask:** is there an architecture where a stale writer is not a reachable state, at lower complexity than the shipped fence + scanners?
**Settled with the author during this review:** restore may mint a new writable workspace; R4 is user-visible only.

---

## Verdict

The fence is the correct mechanism for the contract you actually shipped: restore reanimates the same UUID and the same `<workspaces_dir>/<id>/repo` path. Under that contract a pre-delete writer still holds a valid name for a live writable identity, so Chubby-style generation equality is not optional.

The cheaper structural change is to **stop reanimating the name**. Restore clones a new live workspace (new UUID, new repo path, new collab room). The deleted id stays dead. Then a stale writer can only name a tombstone, ordinary `soft_deleted_at` at write time is enough, and the generation / FenceStamp / Primitive A–B / mint-site scanners can go.

That **removes** the restore-zombie, it does not relocate it. Cost is one restore rewrite plus a delete-time frozen HEAD, not a history-store migration.

You already suspected this in brief §6(c). The honest answer is: yes, same-id restore is the requirement that created the fence. “Restore exactly” can mean content, history through freeze, members, and settings. It does not have to mean the same UUID.

---

## 1. What the fence is actually compensating for

Two facts, both in this repo:

1. Git is a side effect outside the Postgres transaction. `write_and_commit_files` in [`src/call_manager/proposal_assist/citations/gitio.py`](../../../src/call_manager/proposal_assist/citations/gitio.py) runs subprocesses; rollback does not undo a commit.
2. Restore in `restore_workspace_db` ([`src/call_manager/workspaces.py`](../../../src/call_manager/workspaces.py)) keeps the same `Workspace.id`, clears `soft_deleted_at`, and bumps `lifecycle_generation`. `workspace_repo_dir` in [`src/call_manager/proposal_assist/paths.py`](../../../src/call_manager/proposal_assist/paths.py) is `<workspaces_dir>/<workspace_id>/repo`. Same id ⇒ same filesystem target.

A liveness check at commit time cannot express R4 across restore, because restore clears the flag the check reads. That is why Primitive B compares generation **equality** under the exclusive advisory lock, and why “minted at authorization, not next to the write” had to be policed by AST.

The scanners are not a taste failure. They are what you pay when:

- the sink (`write_and_commit_files(repo_dir, files, message)`) does not mention the fence, so position is the only evidence a fence was taken (brief §4a);
- the mint is a public function over a column any `Session` can `SELECT`, so late mint is physically possible (brief §4, “structural diagnosis”);
- there are seventeen doors, not one API server.

Your correction in §4b is right: a late mint immediately before `acquire_commit_mutex_at_generation` is not always a no-op, because the acquire waits and then re-reads. The property you actually need is “the stamped read completed before the mutex acquire began,” and a carried token is only load-bearing when belief and effect are different transactions or processes. The scanners police a wider property than that. That is why they feel expensive relative to the mechanism.

---

## 2. Answers to the brief’s questions

### (a) Make the sink demand the token — RELOCATES, do not do this if the goal is less mechanism

`fenced_commit(stamp, repo_dir, …)` that itself takes Primitive B would let mypy answer “did you pass a stamp” and would delete the positional scanners (the 1,381-line part). It would **not** delete the provenance problem. A caller can still mint from a workspace loaded next to the write and hand that stamp in. You already caught that defect in review: a legitimate call to the legitimate public mint, with a freshly-read workspace.

What goes wrong:

- Every git caller grows a stamp argument. The mint-site convention remains, so the 405-line provenance scanners stay or become a type that Python cannot express.
- The sink still does not know the stamp was minted in a different transaction. Types check presence, not vintage.
- You keep generation, FenceStamp, Primitive B, and job `claimed_at_generation`. Complexity moves from scanners into the call graph.

Do this only if you keep same-id restore and want to shrink scanners. It is not the complexity cut.

### (b) Seal the mint / FastAPI dependency — RELOCATES, and you already know why

A private constructor plus `Depends` returning `(workspace, stamp)` seals routes. It does not seal:

- background job finalizers
- the agent child process
- WebSocket collab handlers
- setup persist
- any service called with a `Session` and a `workspace_id`

The defect you caught was not `FenceStamp(...)`. It was `stamp_for_workspace(freshly_loaded_workspace)`. Sealing the constructor does not touch that. A single door would work (GitLab push, Overleaf POST, k8s apiserver). Collapsing seventeen doors into one is a larger rewrite than clone-on-restore, and revoke-on-delete still has to exist at that door if restore reuses the id.

### (c) Is the requirement the bug? — yes, if “requirement” means same-id restore

**What concrete harm does a zombie commit cause?**

Under **user-visible R4** (settled): a commit nobody can ever see is not a violation. Harm is only if that commit becomes HEAD, a version the UI shows, or a SHA an `ArtifactVersion` / collab checkpoint points at **on a live workspace**.

Under **literal R4** (any mutation after admit-before-delete): a commit to a doomed repo is already a violation, even if later reset. That reading forces a fence, a transactional history store, or a new identity. It is the reading that made 5k lines of scanners rational.

GitLab’s choice (soft-deleted project remains writable through the retention window; they audited the race and accepted it) is a third contract: weaken R4. Given freeze-only delete, the residual harm of a zombie on a **hidden** workspace is “an extra commit an admin can inspect and revert,” plus whatever `reconcile_workspace_git_drift` would backfill if someone opened it. That is small. The harm that is **not** small is the same commit landing **after restore onto the live identity**. Same-id restore is what turns a curiosity on a tombstone into user-visible corruption.

So: do not weaken R4 on the live object. Change restore so the live object is a name the zombie does not have.

Product comparison, stated carefully:

- Google Drive / Notion trash typically restore the same logical item. Users expect the same URL. That is a product choice, not a law of trash.
- GitLab restore is same-id and therefore **relocates** this problem into whatever they use to serialize writes (Gitaly + one push door). You do not have one door.
- Your admin inspect already keys off the deleted UUID. A tombstone that stays deleted and points at `restored_as` is a better admin object than a zombie-writable same id.

URLs, bookmarks, and `/api/ws/workspaces/{id}/{artifact}` will break. There is no redirect table today. For a dozen on-prem users that is acceptable if the restore response returns the new id and the admin UI links it. A public-id redirect over a new incarnation **relocates** the name problem into the redirect table and is weaker; do not add it unless URL stability turns out to be load-bearing after you try without it.

### (d) Is there a name for mint-site proof?

Your working theory is right. Kleppmann’s fencing-token note (and Chubby §2.4 sequencers, Kafka producer epochs / KIP-98, HDFS QJM `lastPromisedEpoch`, NFS `ESTALE` generation) all assume the token is **issued by the lock service over a network hop**. Provenance is free because the client cannot mint `GetSequencer()` / `InitProducerId` / `newEpoch()` from a local integer column.

There is little prior art for “prove this token was captured at the authorization boundary” because systems that need the token do not let the writer mint it. You are linting what everyone else gets from process separation.

A process/RPC boundary (dedicated write daemon that mints on lock grant, like Chubby) would **remove** mint-site scanners. It would **not** remove the epoch. It is new infrastructure on a one-machine hospital box. Last resort, per R5. Clone-on-restore is cheaper and deletes the need for the token.

The one place you already have schema-level provenance — `claimed_at_generation` captured in the same `SELECT … FOR UPDATE` as the claim — is the Chubby shape: mint and lock are one statement. That is why it needed no scanner. After new-id restore, even that column can go, because a claimed job still has `workspace_id` of the tombstone.

### (e) Anything the framing hid?

Yes. Several “different architectures” in the usual survey **do not remove this bug** if restore reuses the writable name:

| Candidate | Verdict |
|---|---|
| History in Postgres / temporal tables / Dolt | **Removes** the git/DB split. Does **not** by itself stop a stale writer from inserting into the live identity after same-id restore unless writes are the same transaction as the lifecycle row **and** the writer cannot hold a pre-restore capability. Huge migration. Rejected on complexity. |
| Durable Objects / single-thread actor per workspace | Serializes. Does **not** kill pre-admit work after same-id restore unless the actor identity changes or an epoch returns. Relocates. Also new infra. |
| OS unlink / inode fencing | Git subprocesses re-resolve paths. After restore-to-same-path the inode trick fails. In-flight processes can still write. Relocates or fails R4. |
| Single-writer queue | Minutes-long LLM turns and hours-long Yjs sessions holding a queue are a product regression. Same queue identity after restore ⇒ epoch returns. Relocates. |
| Repair-at-restore (reset to delete-time HEAD) | Current `reconcile_workspace_git_drift` is **forward-only**. A durable “max valid SHA at delete” watermark is another fence. User-visible flicker if a stale commit was already read. Relocates. Only attractive if same-id is mandatory **and** R4 is user-visible — you have now dropped same-id, so do not build this. |
| Quarantine-on-delete (rename repo) | You already refuted this for same-path restore. Combined with **new id**, quarantine is unnecessary: the new workspace never uses the old path. Optional containment for purge (Slice E), not for restore. |
| In-process capabilities | Without a language/process boundary, sealed objects still need scanners or one door. Does not beat identity split on complexity. |

The framing that hid the cheap answer was “the problem is proving mint site.” The problem is **a capability that remains a valid name after restore**. Fix the name, and mint site stops mattering.

---

## 3. Recommended architecture

Keep git on disk. Keep the commit mutex. Change restore.

### Delete (still one identity)

Under `acquire_commit_mutex` in `soft_delete_workspace_db`:

1. Checkpoint / evict collab into git **before** setting `soft_deleted_at`. Today retirement checkpoints **after** the delete commit (`_RETIREMENT_BARRIER_SECONDS`). That is the wrong order if restore will copy HEAD: a later clone can include post-delete orphans or miss the evict checkpoint.
2. Record `frozen_head_sha`.
3. Set `soft_deleted_at`, cancel jobs, evict rooms. Do **not** bump a generation for the new contract. (While Phase 1 ships behind the existing fence, bumping generation can stay until Phase 2 deletes it.)

After that commit, writers targeting this id lose on `soft_deleted_at`. Orphan objects they still manage to create on the tombstone repo are invisible if restore resets to `frozen_head_sha`. Under user-visible R4 that is enough.

### Restore (new identity)

`restore_workspace_as_clone`:

- Tombstone stays `soft_deleted_at IS NOT NULL` forever (until purge).
- `restored_as_workspace_id` unique FK; second restore returns the same new id.
- New UUID, new directory, new collab room.
- Copy content rows (new PKs, rewritten FKs): members, chat, summaries, artifact versions through frozen HEAD, proposed changes, attachments, phase approvals, references + chunks, citation-audit cache, claim classifications / annotations, spec snapshots.
- Leave on the tombstone: `WorkspaceEvent`, `SpecLockEvent`, `CitationLlmUsage`, all job tables, `YjsUpdate`, `CollabArtifactState`.
- New `WorkspaceSetupJob` via existing `_SETUP_RESTORE_MAP`. Do not resurrect in-flight turns / pass jobs.
- Filesystem: `git clone` of the tombstone repo + `git reset --hard frozen_head_sha`; copy attachment/reference **files by copied row ids**, not a blind `copytree`.
- Failure: leave `restored_as` unset so retry is safe. Do not flip a live new row until the tree is in place (or insert the new row deleted and clear the flag after copy).
- HTTP: 200 + `{source_workspace_id, workspace_id}`. Current 204-empty cannot tell the admin the new id. No redirect table.

This is a product change, not a storage rewrite. There is no existing clone helper; the tree is three subdirs and ~25 child tables. Unique constraint `uq_workspace_setup_jobs_ws` is per `workspace_id`, so a new id is clean.

### Then delete the fence

Once no live writer can name a restored object with a pre-delete id:

- `acquire_commit_mutex` + under-mutex `soft_deleted_at` re-read + `WorkspaceDeleted` stays. That is a liveness lock, not a lifecycle fence.
- Delete `acquire_commit_mutex_at_generation`, `live_at_generation`, `WorkspaceGenerationStale`, `FenceStamp.generation`, `stamp_for_workspace` mint-site rule, `claimed_at_generation`, `bump_lifecycle_generation`, `Workspace.lifecycle_generation`.
- Delete `tests/fencing/` provenance / position / stamp gates. Keep an import-boundary check only if it still owns a remaining git sink.
- Keep lock order M→J→W, `bounded_lock_waits`, room `evicted`, retirement barriers, `reconcile_workspace_git_drift`, scheduler lock.
- If session-ownership still needs `active_turn_job_id` on a tool context, keep that field only. Do not keep generation to smuggle it.

Jobs do not need generation “to be safe.” A cancelled job on the tombstone still has the old `workspace_id`. It cannot write the clone.

---

## 4. REMOVE vs RELOCATE scorecard

| Change | Effect on the zombie | Complexity |
|---|---|---|
| New-id restore + frozen HEAD | **Removes** | Moderate. One restore path, one delete-time SHA, admin API body, row/file copy. Then a deletion of fence code. |
| Sink demands stamp | Relocates mint-site | Low–moderate. Deletes positional scanners only. |
| Seal mint / single FastAPI Depends | Relocates to non-HTTP actors | Low and incomplete, or high if you really build one door. |
| History in Postgres | Removes git dual-write; same-id restore still needs a capability revoke | High. Already costed and rejected in the brief. |
| Write daemon / RPC mint | Removes mint-site scanners; keeps epoch | High. Violates R5 unless everything else fails. |
| Repair-at-restore watermark | Relocates (the watermark is a fence) | Moderate and easy to get wrong. |
| Weaken R4 (GitLab-style) | Removes the *requirement*, not the race | Lowest code change; accepts user-visible history after restore. Author did not choose this. |
| Public-id redirect, new incarnation underneath | Relocates the name into the redirect | Looks like same-URL restore; pre-delete writers that resolve the public id still need an epoch. |

---

## 5. What I would not keep “just in case”

Do not leave `lifecycle_generation` on the row after Phase 2 “in case we revert.” Dead columns become a second authority. If same-id restore ever returns, add the column back with a spec; do not half-delete the fence.

Do not add a redirect table in Phase 1. It is the most tempting way to un-decide the product change, and it reopens the name problem.

Do not copy Yjs onto the new id. The room key is `(workspace_id, artifact)`. A copied log on a new id is a new room pretending to be the old one, with watermarks that do not match the cloned git HEAD. Hydrate from frozen HEAD.

---

## 6. Residual risks (honest)

1. **Clone is a dual write.** DB rows + `git clone` + file copy can fail mid-way. Mitigate with idempotent `restored_as` and “not live until tree exists.” This is one admin path, not seventeen writer paths. That is why it is cheaper than fencing every write.
2. **Stale writer vs clone race.** If delete does not freeze HEAD under the mutex *before* `soft_deleted_at`, restore can copy a post-delete commit. That is a Phase 1 bug, not a reason to keep the fence. Test with a planted orphan on the tombstone.
3. **Purge (Slice E).** A stale `git commit` can recreate a purged directory. New-id restore does not solve purge. You still want a filesystem barrier before `rmtree`. Out of scope for the fence question; do not pretend it is solved.
4. **Session ownership / kill-switch.** Independent of lifecycle. Do not smuggle it through generation.
5. **In-process `WorkspaceLifecycle`.** Two authorities (Postgres vs in-memory retirement) remain a 6-second barrier problem. New-id restore does not fix that, but it also does not need generation inside `_State`. Keep `is_retired()`; drop `applied_generation`.

---

## 7. Migration cost

Working system, dozen users, one machine.

- **Phase 1** (clone-on-restore): localized to [`workspaces.py`](../../../src/call_manager/workspaces.py), [`admin_retention.py`](../../../src/call_manager/web/api/admin_retention.py), models + Alembic, collab checkpoint-on-delete, admin UI, restore tests. Fence stays up, so you are not flying without a net.
- **Phase 2** (delete the fence): mechanical. Convert ~18 `acquire_commit_mutex_at_generation` sites, strip generation from job claims / ToolContext / lifecycle, delete scanners, drop columns. Risk is missing a site that still assumes generation equality. Grep gates: `lifecycle_generation`, `acquire_commit_mutex_at_generation`, `FenceStamp` should go empty except historical docs.

No new service, no history rewrite, no queue, no k8s.

---

## 8. On the brief’s “good answer” test

- The mechanism you shipped is standard. The scanners are the unusual part, and they exist because Python cannot type “minted at the authorization boundary” and because the git sink does not mention the fence.
- The one structural change that removes most of them is **not** `fenced_commit`. It is **stop restoring onto the name a stale writer holds**.
- You gave yourself this problem by interpreting R3 as same-UUID restore while git remained a non-transactional sink. Weakening that interpretation is the simple fix. Weakening R4 on the live object is the wrong fix.

Implementation checklist lives in the session plan (`Restore as new identity`). This note is the argument, not the task list.

## Related

- Brief: `/home/parlar_ai/fence-problem-brief.md`
- Same-day Sol review (different recommendation: public id + incarnation + mailbox): [2026-08-25-sol-fence-elimination-architecture-review.md](2026-08-25-sol-fence-elimination-architecture-review.md)
- Current same-id restore: [2026-08-16-admin-retention-surface-design.md](../specs/2026-08-16-admin-retention-surface-design.md)
- Current fence: [2026-08-17-lifecycle-fence-design.md](../specs/2026-08-17-lifecycle-fence-design.md)
