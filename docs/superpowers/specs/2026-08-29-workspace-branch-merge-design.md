# Workspace Branch Merge — Design (INCOMPLETE)

**Date:** 2026-08-29
**Status:** **NOT ready to plan.** Split out of
`2026-08-29-workspace-branches-design.md` on 2026-08-29 after three outside
review rounds, because merge was not converging while the switch design was.
**Depends on:** the branches design (create, switch, delete). Merge is meaningless
without branches to merge; branches are useful without merge.

---

## 1. Why this is a separate document

Three review rounds on the combined spec produced 25 findings. They did not land
evenly:

| | switch (§3-§6 there) | **merge (this document)** |
| --- | --- | --- |
| round 1 | 2 | **5** |
| round 2 | 3 | **3** |
| round 3 | 3 | **5** |
| **total** | **8** | **13** |

The switch findings narrowed each round — by round 3 they were a lock timeout, a
generation interaction, and a wrong row in a failure table. The merge findings did
not narrow. Round 3's merge CRITICAL was that the design **never mentions the live
document at all** — a missing foundation, three rounds in.

The reason is now clear. Merge silently depends on three subsystems this document
never designed against: the collaborative editor's handover, the citation evidence
ledger, and section identity surviving conflict resolution. Each is comparable in
size to the switch design. Patching them one review round at a time was producing
a new foundational gap per round.

**So this is parked at the design stage, deliberately.** What follows is real work
— the section-level unit, the merge base, the three-way tables — and it is all
still valid. It is not finishable without resolving §2 first.

## 2. Unresolved — these block planning

Every one is a confirmed finding, verified against the code, not yet designed.

### 2.1 The merge never reaches the live document (CRITICAL)

§4 composes markdown and commits it to git. It says nothing about the Yjs room,
the update log, or hydration.

The live document is not git (§2 of the branches design). After a merge the
editor still holds the **pre-merge** document, and the next checkpoint rebuilds
markdown from `yjs_updates` and commits it (`checkpoint.py:137-146`) —
**overwriting the merge.** The merge disappears with no error.

Merge needs a handover comparable to the switch's §3.2: tear the rooms down,
truncate, commit, let hydration re-seed from the merged HEAD. Whether it can
reuse that sequence or needs its own is undesigned.

### 2.2 The ledger merge can drop a row the user's own choice requires

§3.5's table drops a row deleted on the target and kept on the source
(`yes / no / yes`). But if both branches changed the containing section and the
user picks the **source** version, that marker survives in the prose while its
row is dropped — and §3.5's own postcondition then aborts the merge the user just
resolved.

Set semantics on the ledger and choice semantics on the sections are not
independent, which is exactly what §3.5 claims they are. The rows kept must be
derived from the **final** section choices, then deletion applied — an ordering
the document does not have.

### 2.3 The postcondition checks the wrong key

§3.5 asserts every marker's `ev_id` has a ledger row. **Export resolves by
`citekey`**: `_ordered_unique_citekeys` feeds `find_by_citekey`
(`bibliography.py:453-462, 487-489`). A marker whose `ev_id` row carries a
different `citekey` passes the stated assertion and is still `unresolved_citekey`
at export. The assertion must match `id` **and** `citekey`.

(Note also that the ledger row's field is `"id"`, not `"ev_id"` —
`ledger.py:93-94`. `find_by_id` takes the ev id.)

### 2.4 `section_path` does not mean what §3.5 says

§3.5 says a conflicting row is surfaced "with the section that owns it
(`section_path`)". It is not the proposal's section. It is copied from
`chunk.section_path` — a section of the **cited source document**
(`human_cite.py:216-223`) — and stored inside `locator`
(`ledger.py:97`). Ownership has to be found by locating the row's `ev_id` marker
in the proposal and sectionizing its position.

### 2.5 Placement identity does not survive conflict resolution

§3.6 places an arriving section after its predecessor keyed on
`(heading, occurrence)`. But §3.3 declares that key **unusable** for a heading
group whose counts differ — which is precisely a group that went to conflict. After
a keep-both resolution the occurrence numbers are new, and predecessor identity is
undefined.

Merge items need explicit temporary identities carried through resolution, and
placement computed from those, not from heading occurrence.

### 2.6 `list_commits` needs `--first-parent`

Carried over from the branches design, where it is out of scope precisely because
nothing writes a two-parent commit until merge does. `list_commits` runs `git log`
with no revision argument (`gitio.py:525-533`), so it follows HEAD. The moment
§3.7 writes a merge commit, the version list silently gains every commit from the
merged branch. One flag, but it belongs to this work.

---

## 3. The design so far

### 3.1 The unit is a section, not a line

`editing/sectionizer.py` splits an artifact into `##`-headed sections. Each
`Section` carries a content `sha` (`sectionizer.py:30`), and the module's stated
invariant is that `"".join(s.text for s in split_sections(doc)) == doc`
byte-identically.

Merging therefore composes a document by concatenating chosen sections, with no
reconstruction step and no risk of losing bytes between them.

`git merge` was rejected. Git merges by line; two edits inside one paragraph of
Specific Aims produce conflict markers **inside prose the user exports to a
funder**, and git has no concept of a section.

### 3.2 A merge base is required

Comparing the source branch against the target can only say **equal or
different**. It cannot say *which side changed*, so "changed on one side only"
is undefined without a third point of reference.

The merge therefore reads the artifact at **three** commits:

- **base** — `git merge-base <target> <source>`, a new `gitio` helper
- **target** — the branch being merged into
- **source** — the branch being merged

Each is read with the existing `blob_sha(repo_dir, ref, path)` (`gitio.py:324`,
which already accepts an arbitrary ref) and `read_blob` (`:346`), then split with
`split_sections`.

### 3.3 Matching

Sections are matched by **heading**, not by index. Index is positional and shifts
whenever a section is added or removed; heading survives reordering.

Where a heading appears more than once **and the number of occurrences is the
same in base, source and target**, sections are matched by
`(heading, occurrence number)`: the second `## Budget` pairs with the second
`## Budget`.

**Where that count differs between any two of the three, every section with that
heading is surfaced as a conflict for the user to resolve, and none of them is
paired automatically.** Positional pairing is wrong exactly when it looks
plausible: if one branch deletes the first of two `## Budget` sections, the
survivor becomes occurrence 1 and would be silently paired with — and treated as
an edit of — the other branch's untouched first section.

Nothing in the app prevents duplicate headings, though neither real workspace
document has any (6/6 and 4/4 unique, measured 2026-08-29).

### 3.4 The three-way outcome

For each matched section, compare `Section.sha` at base, source and target:

| base vs target | base vs source | Behaviour |
| --- | --- | --- |
| same | same | Nothing changed. Keep it. |
| same | **differs** | Changed on the source only. **Take it.** No prompt. |
| **differs** | same | Changed on the target only. **Keep it.** No prompt. |
| **differs** | **differs**, and source ≠ target | Changed on **both**. The user chooses: theirs, mine, or keep both. |
| **differs** | **differs**, and source == target | Both made the same edit. Keep it. No prompt. |

Presence is resolved the same way against base:

| In base | In target | In source | Behaviour |
| --- | --- | --- | --- |
| no | no | yes | Added on the source. User chooses: take it or not. |
| no | yes | no | Added on the target. Keep it. |
| yes | yes | no | Deleted on the source. User chooses. |
| yes | no | yes | Deleted on the target. User chooses. |
| yes | no | no | Deleted on both. Stays deleted, no prompt. |
| no | yes | yes | Added on both. Conflict if the bodies differ. |

### 3.4.1 Renames are shown as a delete plus an add

Renaming a heading breaks the match: the tool sees one section removed and
another added. **This is accepted and displayed honestly** — the user sees both
entries and resolves them.

Content-similarity guessing ("did you rename this?") was rejected for now: it is
additive later and needs no data change. A stable per-section id was rejected
outright, because it means writing machine markup into `proposal.md`, the file
the user exports to the funder.

### 3.5 The citation ledger merges with the document, in the same commit

**Merging `proposal.md` alone corrupts every citation the merge brings across.**

`citations.json` is a separate file. The whole citation subsystem is built on the
two moving together: `gitio.py`'s own module docstring says the atomic commit
exists "so `proposal.md` and `citations.json` always move together", and both
writers honour it in one `write_and_commit_files` call —
`human_cite.py:239-243` and `citations/core.py:629-634`. Export resolves the
markers in the prose against the on-disk ledger (`export.py:72-97`), and
`build_bibliography` renders only what the prose actually cites.

So a merge that takes a cited section from the source branch, while committing
the target branch's ledger, produces prose whose markers have no evidence rows:
the export either drops them or raises. The bug appears at export time, in front
of the funder-facing document, long after the merge looked successful.

**The ledger is merged as a set of rows keyed by `ev_id`**, not by section.
`ev_id` is `ev-<12 hex of uuid4>` (`ledger.py:50`) — assigned once at creation
and never recomputed — so three-way set semantics against the base are well
defined and need no heading matching at all:

| In base | In target | In source | Behaviour |
| --- | --- | --- | --- |
| no | no | yes | Added on the source. Take it. |
| no | yes | no | Added on the target. Keep it. |
| yes | yes | yes, identical | Unchanged. Keep it. |
| yes | yes | yes, **differing bodies** | Conflict. Surface with the section that owns it (`section_path`). |
| yes | no | yes | Deleted on the target. Drop it. |
| yes | yes | no | Deleted on the source. Keep it. |

Rows are keyed independently of the section decisions on purpose. A row taken in
whose section was **not** taken becomes an orphan — evidence for a claim not in
the document — and that is safe: `build_bibliography` parses the prose for
markers, so an unreferenced row renders nothing. The reverse (a section taken
without its rows) is the unsafe direction, and set-union on `ev_id` cannot
produce it.

**The invariant is checkable, not aspirational.** The marker in the prose carries
its own `ev_id` — `insert_marker` writes it (`markers.py:83`) and `find_markers`
reads it back (`markers.py:45`) — so the merge has a postcondition it can assert
directly before committing:

> every `ev_id` returned by `find_markers(merged_proposal)` has a row in the
> merged ledger.

A merge that cannot satisfy it must fail **before** the commit, not produce a
document that breaks at export. It is why the rule is set-union rather than anything cleverer: a cleverer rule
would need this check too, and could still fail it.

**The merged `proposal.md` and the merged `citations.json` must be written in one
`write_and_commit_files` call**, preserving the invariant the rest of the
subsystem depends on. A merge that commits them separately is a defect even if
both commits succeed.

`references.citekey` is a database cache of this ledger and can drift; the repair
path already exists (`reconcile.py:183`) and should run after a merge.

### 3.6 Order of the merged document

The **target** branch's order — the branch being merged into.

A section arriving from the source is placed after its **source-branch
predecessor**, identified by `(heading, occurrence number)` — the same key §3.3
matches on, not the bare heading. Identifying the predecessor by heading alone is
ambiguous exactly where §3.3 permits duplicates: "after `## Budget`" does not say
which `## Budget`.

Three rules settle the remaining cases:

- **Several sections arriving after the same predecessor keep their source order**
  relative to each other.
- **If the predecessor is not present in the merged document** — absent from the
  target, or deleted by the merge — the arriving section falls back to the next
  earlier source predecessor that *is* present, and goes last if none is.
- **If the predecessor is itself conflicted** and the user has not yet resolved
  it, placement is deferred until they do. Placement is never computed against a
  section whose presence is undecided.

### 3.7 The merge commit has two parents

The merged content is composed by the app, not by git — but the commit records
both parents, written by setting `MERGE_HEAD` before committing.

**`write_and_commit_files` cannot write this commit.** It short-circuits when the
staged tree is unchanged: `git diff --cached --quiet` returning 0 makes it return
the existing HEAD **without committing at all** (`gitio.py:177-195`). A merge
resolved entirely to target content — every section kept, which is a perfectly
ordinary outcome — stages nothing. The result is silent and bad: no merge commit,
no second parent, the branches never actually joined, and the next merge redoing
all the same work because the ancestry says nothing happened.

The merge therefore needs its **own commit path** in `gitio`, not a flag threaded
through the general one. It sets `MERGE_HEAD`, stages, and commits with
`--allow-empty` so an unchanged tree still produces a commit. The empty-diff
short circuit stays exactly as it is for every other caller, where skipping a
no-op commit is the correct behaviour.

**`MERGE_HEAD` must be removed in a `finally`, on success and on every failure
path.** It is a state file git leaves in place; `write_and_commit_files` restores
files and resets only the paths it touched (`gitio.py:216-292`) and knows nothing
about it. A `MERGE_HEAD` left behind after a failed merge turns the **next**
ordinary commit — a section save, a checkpoint — into an unintended merge commit
with a second parent nobody chose.

The graph cannot draw a fork today (§9), so this changes nothing visible. It is
recorded because **ancestry that is not written down cannot be recovered**, while
not drawing it yet is a UI gap fixable at any time.

