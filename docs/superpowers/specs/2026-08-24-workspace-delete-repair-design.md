# Workspace delete: quarantine and repair — Design

**Status:** proposed, 2026-08-24. Supersedes the *approach* of the epoch-fence
programme for the workspace-delete lifecycle. Does not propose reverting anything
already merged.

## 1. The lifecycle we are protecting

Confirmed with the user, 2026-08-24:

- the **owner** presses delete — the workspace is **disabled and hidden**, nothing is
  destroyed
- a **system admin** can list it, inspect it, and **bring it back**
- **only** the system admin can really delete it

It is a safety feature. "Delete" and "disable" are the same operation; there is no
separate disable to build.

Restore's contract follows from that: **give the owner back what they had at the moment
they pressed delete.**

## 2. What is already true (measured at `cf6e9a24`)

Load-bearing facts. Each was executed or read, not recalled.

| Fact | Evidence |
|---|---|
| Delete destroys zero bytes | the route never calls `scrub_workspace_bytes` |
| `lifecycle_generation` moves in exactly **two** places | `workspaces.py:227` (delete), `:331` (restore) — no other caller |
| Delete records **nothing** about git HEAD | no `current_head` in `workspaces.py` |
| Restore does **not** touch the repo | `restore_workspace_db` clears the flag, bumps the generation, writes audit |
| Every git subprocess runs with `cwd=repo_dir` | `citations/gitio.py:167` |
| The repo path is computed fresh per call, never cached | `proposal_assist/paths.py:12` |
| A missing `cwd` raises `FileNotFoundError` **before git starts** | executed |
| The LLM runs as a **child process** and delete already **reaps** it | `drafting/claude_adapter.py` `interrupt`/`terminate`/`_force_kill_subprocess`/`_reap_subprocess`; wired via `main.py:201` `"turns" -> registry.retire_workspace` |
| `YjsUpdate.id` is a monotonic BigInteger PK, already used as a watermark | `collab/checkpoint.py:99` `max_id`, `:122` `up_to_update_id` |
| Admin hard-delete does **not** exist | `scrub_workspace_bytes` has zero production callers |
| `scrub_workspace_bytes` removes attachments + references, **not the repo** | its body |

### 2.1 The zombie population is smaller than assumed

The epoch-fence programme rests on "a stale actor may write git after delete". True, but
the population was overstated. Of 32 `run_in_threadpool` call sites, almost all are HTTP
**request bodies** (`_route`, `_gate`, `_read`, `_write`, `_body`, `_git`) — FastAPI's
idiom for calling synchronous DB/git code from an async route. They are seconds long and
end on their own.

The genuinely long-running actor is the LLM, and it is **already terminated and reaped on
delete**. What remains unkillable is narrow: reference ingestion
(`tools/references.py:_do_fetch_and_ingest`) and any request handler mid-flight at the
instant of delete.

## 3. The design

Two mechanisms. Neither needs to reach individual call sites.

### 3.1 Quarantine — rename the repo at delete

Inside `soft_delete_workspace_db`'s existing transaction, while it already holds the
commit mutex, after the DB state change:

```
mv <workspaces_dir>/<id>/repo  <workspaces_dir>/<id>/repo.frozen-<generation>
```

One atomic rename. Because the repo path is resolved fresh on every call and every git
subprocess passes it as `cwd`, **every subsequent git write fails at spawn** with
`FileNotFoundError` — before git runs. This covers every writer: mutex-taking or not,
catalogued or not, thread or process. It does not care how many call sites exist.

`repo.frozen-<generation>` is the artefact the admin inspects and restore brings back.

### 3.2 Repair — watermark at delete, rewind at restore

Rename cannot stop a git subprocess **already spawned**: on Linux it follows the inode
and writes into the renamed directory. That window is bounded by gitio's own documented
worst case (~180s).

So, in the same transaction, record two watermarks per artifact:

- `head_sha` — `current_head(repo_dir)`
- `max_update_id` — `max(YjsUpdate.id)` for the workspace

The mutex is held, so this is a consistent snapshot.

At **restore**, under the mutex:

1. rename `repo.frozen-<generation>` back to `repo`
2. `git reset --hard <head_sha>` then `git clean -fd`
3. `DELETE FROM yjs_updates WHERE workspace_id = ? AND id > max_update_id`
4. drop `ArtifactVersion` rows past the watermark so version history matches git
5. clear `soft_deleted_at`, bump generation, commit
6. evict any cached room so it re-hydrates from the truncated log

The owner gets back exactly the delete-time state, **by construction** — a stronger
guarantee than the fence offers, which prevents new commits but never defines a restore
point.

### 3.3 What is deliberately NOT rewound

12 tables carry `workspace_id` + `created_at`. Only two are the document.

- **git + the Yjs log** — rewind
- **`WorkspaceEvent`, `SpecLockEvent`** — audit. Keep the zombie writes; deleting audit is
  worse than keeping it
- **the other eight** (references, citation usage, claim classifications, …) — derived
  analysis. Stale rows are cosmetic and regenerate

A blanket "delete rows after T" would be wrong.

## 4. What this does not solve

Stated so it is not discovered later.

1. **Cloud-token burn** from a job still running — already handled by delete cancelling
   jobs and reaping the agent. Orthogonal.
2. **`review/pass_runner.py:349` writes git with no mutex (MEASURED: zero `acquire_commit_mutex*` calls in that file).** Quarantine covers it (the
   path is gone); the epoch fence never could, because a writer taking no mutex is
   invisible to it.
3. **Readers of a deleted workspace's repo must learn the frozen path** —
   `admin_retention.py:282` resolves `workspace_repo_dir` today and would get
   `FileNotFoundError`. Needs a resolved-path helper and a clean error contract.
4. **A zombie can still insert DB rows.** Mostly harmless; audit rows are wanted.
5. **If `lifecycle_generation` ever moves for a reason other than delete/restore**, repair
   stops working — you cannot rewind a live document with legitimate writes interleaved.
   Today it moves in exactly two places. **This is the premise to challenge before
   accepting this design.**

## 5. Consequence for the epoch-fence programme

`F1`–`F3e1a2` are merged and stay. Repair *uses* the commit mutex and the generation
counter that work built. Nothing is reverted.

The proposal is to **stop after what is merged** rather than build `F3e1b-1`, `F3e1b-2`,
`F3e2`, `F3f`, `F4`–`F9`.

The size argument, measured:

| | lines |
|---|---|
| Fence mechanism (`fencing.py` + `ports/`) | ~1,067 |
| Gates policing it (`tests/fencing/`) | **4,993** |
| Git write sites protected | 12 |
| Remaining `_live` callers to convert | 18 |
| **This design** | **~100** |

The gates were migration scaffolding whose job was to hold a line while 18 call sites were
converted. Scaffolding should shrink toward zero as the migration completes. Instead it
grew: 19 revisions in one day, each teaching an AST walker one more Python construct
(`AnnAssign`, lambdas, decorators, annotations, class bases). That list has no natural end,
because it is a reimplementation of "what does Python evaluate, and when".

If the fence is ever completed, the correct ending is to **delete
`acquire_commit_mutex_live`**, at which point calling it is a `NameError` and all 4,993
lines can go. The interpreter becomes the gate.

## 6. Also missing from the user's model

Not part of this design, but discovered while checking it: **admin hard-delete is not
built.** `scrub_workspace_bytes` exists, is tested, and has zero production callers — and
it removes attachments and references but **not the repo**. Completing the safety feature
needs a purge that also removes `repo/` and any `repo.frozen-*`.

## 7. Open questions

1. Does `lifecycle_generation` move for anything other than delete/restore, now or by
   design? (§4.5 — decides whether this design is sound.)
2. Should the admin **see** post-delete zombie content while inspecting, or should
   inspection show the delete-time state? Showing it is arguably evidence; hiding it is
   arguably cleaner.
3. Should background work move out of the web process entirely? Independent of this
   design, and argued on its own merits (crash isolation, real cancellation) — not as the
   fix for zombie writes.

## 8. Size

One new table (or JSON column), ~40 lines in delete, ~60 in restore, a resolved-path
helper for readers, and tests. One slice.
