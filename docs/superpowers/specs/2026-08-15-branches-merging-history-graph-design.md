# Grant Lab — Branches, Merging, and History Graph

**Date:** 2026-08-15  
**Status:** Product and architecture specification; not yet implemented.  
**Parent direction:** [Assistant-Led IDE Product Direction](2026-08-15-assistant-led-ide-design.md)  
**Scope:** Human-facing version history, named branches, Git-backed isolation,
comparison, selective transfer, restore, and merge behavior for Grant Lab
applications.

## 1. Decision

Grant Lab will evolve from a linear checkpoint list into a version-control surface
that supports:

1. an interactive commit graph similar to the graphs in established Git tools;
2. named application **branches** with isolated Git-backed working state;
3. isolated concurrent editing backed by Git worktrees;
4. comparison and selective transfer between versions or branches;
5. section-aware, evidence-aware merging;
6. recovery by forward commits rather than destructive history rewriting.

The user-facing concepts are **version**, **branch**, **compare**, **restore**, and
**merge**. “Branch” is intentionally not renamed: it accurately describes the graph
relationship and is already established in IDEs and version-control tools. Low-level
refs, worktrees, commit parents, and SHAs remain available in technical details, but
they do not lead the normal workflow.

The graph is not merely a visualization. It is the primary place to understand how
the application evolved and to initiate version operations with explicit scope and
consequences.

## 2. Why Grant Lab needs branches

Grant applications often need parallel exploration before a decision is made. Useful
examples include:

- a conservative and an ambitious framing of the aims;
- alternative methodological approaches;
- a shorter version for a strict page limit;
- a funder-specific adaptation of a shared scientific idea;
- a collaborator's proposed restructuring;
- an assistant-generated experiment that should not disturb the main application;
- a pre-review line preserved while review findings are addressed elsewhere.

Ordinary undo is not sufficient because these alternatives may live for days, receive
independent edits, and need to be compared or combined later. Copying an entire
workspace is also insufficient: it obscures their common origin and makes later
integration, provenance, citations, and review state difficult to reason about.

Branches provide controlled divergence inside one application.

## 3. Terminology and mental model

| User-facing term | Meaning | Technical representation |
|---|---|---|
| Live changes | Durable edits not yet represented by a named checkpoint | Yjs updates after the branch's checkpoint watermark |
| Version | An immutable checkpoint of the application state | Git commit plus version/provenance metadata |
| Main | The authoritative line intended for submission | Protected `main` branch |
| Branch | A named alternative line of work | Managed Git branch with a stable branch ID |
| Compare | Inspect differences without changing either side | Commit/section-aware diff |
| Apply from… | Bring selected changes into the current branch | Validated section-level patch followed by a new commit |
| Restore | Make an earlier state current without deleting later history | New forward commit restoring selected content |
| Merge | Combine two divergent branches with a common ancestor | Validated two-parent merge commit |
| Archive | Remove a branch from normal navigation while retaining history | Branch status change; branch retained according to policy |

The primary mental model is:

> Main is the application we intend to submit. A branch is a safe place to explore
> another direction. Versions are durable points on either line. Merging brings an
> approved result back without erasing how it was produced.

## 4. Product principles

1. **Main is protected.** An assistant or background process cannot merge into Main
   without an authorized human action.
2. **A branch contains a coherent application state.** It is not only an alternative
   `proposal.md` file.
3. **The graph explains consequences.** Operations begin from selected graph nodes
   and name their source and destination.
4. **No silent history rewriting.** Restore, apply, and merge create new commits.
5. **Merges understand proposal structure.** Raw line-based conflict markers never
   appear in the normal editor.
6. **Evidence integrity travels with prose.** Citation markers and their ledger data
   are evaluated together.
7. **Derived results are explicitly current or stale.** Readiness and review results
   never silently describe a different version.
8. **Parallel work is truly isolated.** Live edits, assistant proposals, analysis,
   and checkpoint watermarks cannot leak between branches.
9. **Technical details remain inspectable.** Advanced users may reveal branch refs,
   commit SHAs, parent commits, and worktree health.

## 5. Location in the IDE

History remains available as a group in the left navigator and as the **History**
group in the bottom workbench. Opening the full history promotes it to the central
working surface so the graph has enough room to be useful.

The application header gains a persistent branch selector:

```text
Digital Pathology Application     Branch: Main ▾     Due 30 Sep
```

The selector lists:

- Main;
- active branches;
- archived branches behind an expander;
- Create branch…;
- Compare current branch with Main;
- Manage branches….

The active branch is part of the shared IDE context. The navigator, artifact bar,
editor, assistant, checks, sources, versions, and activity all resolve against it.
Changing branches must never silently discard live edits or move the assistant's
pinned context.

## 6. History graph

### 6.1 Default presentation

The full History surface combines a branch graph with a readable version table. It
uses the familiar lane-and-row grammar found in Git graph tools, including VS Code's
Source Control Graph, while adding Grant Lab's artifact and evidence context:

```text
HISTORY                                              Search history…   Filter ▾

Graph        Version                                      Author       Time
●─┐          Merged “Ambitious aims” into Main            Anna         14:42
│ ●          Ambitious aims · Added translational aim     Assistant*   14:10
│ ●          Reworked expected impact                     Anna         13:58
● │          Main · Addressed implementation warning      Anna         13:41
● │          Main · Budget narrative checkpoint           Luis         13:20
│/
●            Main · Before panel review                    Anna         Yesterday

* Proposed by assistant, accepted by Anna
```

Each row is one immutable commit. Lines communicate parent relationships. Labels
identify Main, branch tips, merge commits, named checkpoints, and submission/export
milestones.

The graph supports both compact and expanded modes:

- **Compact:** collapse long uninteresting linear runs while retaining milestone,
  branch, merge, restore, and selected nodes.
- **All versions:** show the complete graph with virtualized rows.
- **List:** an accessible non-graph alternative sorted by time or grouped by branch.

### 6.2 Live-changes row

When the current branch has durable collaborative changes after its last Git
checkpoint, a special row appears above its tip:

```text
┊ ◉  Live changes · Synced, not checkpointed       Anna + Luis      now
│ ●  Main · Last saved version                                      14:42
```

The dotted connector and distinct node shape communicate that this state is durable
but not yet an immutable version. Selecting it explains the distinction and offers
**Save a version**. It is not assigned a fake commit SHA and does not appear as an
ordinary version.

### 6.3 Row content

Every visible row should answer:

- Which branch contains this version?
- What meaningful action produced it?
- Who authored or approved it?
- When did it happen?
- Is it current, stale, merged, restored, exported, or associated with a review?
- Which artifacts or sections changed?

Normal rows display a human-oriented title. Generated strings such as
`collab checkpoint: draft` are retained in technical details, not used as the primary
label when richer provenance exists.

Icons or badges may identify:

- human checkpoint;
- accepted assistant proposal;
- merge;
- restore;
- citation-only change;
- requirements lock/unlock;
- review or readiness milestone;
- export/submission milestone;
- reconciled system record.

Color is used to distinguish lanes, but never as the only signal. Every lane has a
text label at its visible tip, merge edges use directional affordances, and node
shapes/icons retain meaning in high-contrast and color-blind modes.

### 6.4 Selection and details

Selecting a node opens a details pane without changing the current editor state:

```text
Addressed implementation warning
Main · 15 Aug 2026, 14:42 · Anna

Changed
  Application / Implementation       +84 −31 words
  Citation ledger                    1 citation added

Triggered by
  Readiness / Feasibility warning

Checks at this version
  Citation audit passed
  Readiness is now stale

[Open version] [Compare…] [Apply from…] [Restore…] [Create branch…]
```

Technical details disclose the full commit SHA, parent SHA or SHAs, branch ref,
recorded event IDs, checkpoint authors, and reconciliation status.

Double-clicking or activating **Open version** enters a read-only historical mode.
The header and editor clearly show:

```text
Viewing version from 15 Aug, 14:42 — read only
[Return to Main] [Compare with current] [Restore as new version]
```

Historical viewing must not impersonate a branch checkout and must not allow edits.
Creating a branch or restoring produces an editable current state.

### 6.5 Graph interactions

Primary actions are available from a row's context menu and the details pane:

- Open version;
- Compare with current;
- Compare with…;
- Create branch from here;
- Apply selected changes to current branch;
- Restore as new version;
- Copy commit SHA;
- View technical details.

Branch-tip nodes additionally offer:

- Open branch;
- Compare with Main;
- Merge into Main;
- Rename branch;
- Archive branch.

Drag-and-drop merging is deliberately excluded. A graph edge is too small a target
for an operation with application-wide consequences, and dragging does not provide
enough room to explain source, destination, conflicts, and validation results.

### 6.6 Navigation and accessibility

- Arrow keys move between visible version rows.
- Left and right move between graph lanes or collapse/expand grouped runs where
  applicable.
- Enter opens version details; a separate command opens the read-only version.
- Every graph relationship is also expressed in accessible row text such as
  “merge commit with parents Main at … and Ambitious aims at …”.
- The List view is functionally equivalent to the graph and is not a reduced mode.
- Zoom changes lane spacing and row density, not browser text scaling.
- Filters and selection are reflected in the URL so browser back/forward works.

## 7. Creating and working in a branch

### 7.1 Creation

A writer may choose **Create branch** from the current state or any immutable
version. The dialog contains:

```text
Create a branch

Name        [More ambitious aims                    ]
Purpose     [Explore a high-risk/high-reward framing]
Based on    Main · Addressed implementation warning

[Cancel]                                  [Create and open]
```

User-supplied names are labels, not filesystem paths or Git refs. Grant Lab generates
an opaque stable branch ID and a safe internal ref.

If the selected source has live changes, the user must first checkpoint them. Grant
Lab may offer **Save version and create branch** as one explicit operation.

### 7.2 Assistant-created exploration

The assistant may recommend a branch when a requested change would alter the central
aim, structure, evidence strategy, or multiple dependent artifacts:

> This would substantially change the project's central aim. I can explore it in a
> separate branch while keeping Main unchanged.

Creating the branch requires user confirmation. The assistant may then create
proposed changes inside that branch. It cannot merge them into Main automatically.

### 7.3 Isolation

Each branch has independent:

- draft, requirements, plan, citation ledger, and generated Git artifacts;
- collaborative document and presence rooms;
- checkpoint watermark;
- pending assistant proposals;
- current checks, readiness results, and expert reviews;
- activity and version views.

The source/reference library and uploaded source files remain workspace-shared. Their
**use** in the draft, citation ledger, and derived audits is branch-specific. Stable
workspace-level citekeys should be reused so moving cited prose between branches does
not rename the source gratuitously.

Workspace lifecycle, opportunity, deadline, membership, and high-level permissions
remain shared unless a later spec introduces branch-specific sharing.

## 8. Comparing versions and branches

Comparison uses a common structure across the Application, Requirements, citations,
and derived state.

The comparison header always identifies direction:

```text
Compare
From: Main · Before panel review
To:   Ambitious aims · Current
```

The summary groups changes by artifact and section:

```text
Application       4 sections changed
Requirements      1 section changed
Citations         2 added · 1 removed
Checks             3 results stale
Expert review      not run on destination
```

Within prose, users can switch between:

- side-by-side sections;
- inline word-level changes;
- final rendered destination;
- requirements and evidence affected by the selected change.

The default comparison is semantic and section-aware. A raw Markdown diff remains
available in technical view.

## 9. Applying selected changes

Users often need one useful section from a branch without merging the whole branch.
**Apply from…** is therefore a first-class operation.

The user selects one or more changes, reviews the destination, and creates a new
commit on the current branch. Grant Lab revalidates the selection against the live
destination before applying it.

Rules:

1. Applying a section includes its citation markers and required ledger entries.
2. Existing destination citations are preserved unless the selected change explicitly
   replaces or removes them.
3. If the destination section changed after comparison opened, the operation stops
   and refreshes the comparison.
4. Requirements changes show affected application sections before confirmation.
5. The resulting version records the source branch and commit.
6. Applying changes is never represented as if the two branches were fully merged.

## 10. Restoring a version

Restore is forward-only:

```text
Current history: A — B — C
Restore B:       A — B — C — R
```

`R` contains the selected content from `B` and records that it restored from `B`.
Commits `C` and its descendants remain inspectable.

The restore dialog supports:

- restore the full application state represented by the version;
- restore selected artifacts;
- restore selected sections.

It summarizes what will change and which derived results will become stale. Restore
requires a confirmation and creates a checkpoint before applying if the current
branch has uncheckpointed live changes.

Destructive reset, force-push, reflog recovery, and history rewriting are not exposed.

## 11. Merging branches

### 11.1 Merge preparation

A merge always names a source and destination:

```text
Merge “Ambitious aims” into “Main”
```

Before analysis, Grant Lab:

1. verifies permission to merge into the destination;
2. checkpoints live changes on both branches with explicit confirmation where needed;
3. resolves their common ancestor;
4. freezes or queues destination writes for the short finalization window;
5. computes artifact- and section-level changes from the common ancestor;
6. runs deterministic structural and citation validation.

The preview is immutable and tied to exact source, destination, and base commit SHAs.
If either tip changes before confirmation, the preview becomes stale and must be
recomputed.

### 11.2 Automatic classification

For each section or structured record:

| Base → Main | Base → Source branch | Default result |
|---|---|---|
| unchanged | changed | Take source-branch change |
| changed | unchanged | Keep Main change |
| same change | same change | Keep once |
| changed | changed differently | Requires resolution |
| deleted | changed | Requires resolution |
| changed | deleted | Requires resolution |

Sections are identified by stable section identity where available, with heading and
position used as supporting signals rather than sole identity. Renames, splits, and
joins must be shown explicitly.

### 11.3 Merge review

The merge screen begins with a decision summary:

```text
Merge “Ambitious aims” into Main

3 sections changed only in the source branch
2 sections changed in both and need a decision
1 citation requires verification
2 readiness results will become stale

[Review 2 conflicts]
```

For each conflict, the user can:

- keep Main;
- take the source branch;
- edit a combined result;
- ask the assistant to propose a combination;
- postpone the conflict and leave the merge uncommitted.

An assistant-combined result is visibly a proposal and retains the normal accept or
revise boundary. The assistant receives the common base, both versions, linked
requirements, relevant evidence, and the user's merge intent.

### 11.4 Cross-artifact validation

Before the merge can be finalized, Grant Lab checks:

- valid application and requirements structure;
- citation-marker and ledger consistency;
- citekey stability and source availability;
- requirements-to-section coverage affected by the merge;
- requirements lock rules;
- section and document limits;
- proposed changes based on superseded section versions;
- readiness, coherence, claim, and expert-review results that must become stale;
- exported or submission-marked versions affected by the new head.

Warnings may allow an explicit merge; structural corruption, unresolved conflicts,
missing citation ledger entries, or a changed merge tip block it.

### 11.5 Merge result

A successful merge creates one two-parent Git commit on the destination and records:

- source and destination branches;
- exact parent and common-base commits;
- initiating and approving users;
- resolved conflicts and chosen resolutions;
- assistant involvement, if any;
- validation results;
- affected artifacts and sections;
- results invalidated or re-enqueued.

The source branch remains available after merging. The success screen offers
**Archive source branch**, but does not archive it automatically.

The graph displays the merge edge immediately.

### 11.6 Failure and abort behavior

- Preview and validation do not modify either branch.
- Cancelling a merge leaves both branches unchanged.
- A failure during finalization restores the destination worktree and index to their
  pre-merge state and leaves no partial database resolution.
- A Git commit that lands before a database failure is reconciled forward using the
  existing Git-to-database recovery model.
- Raw conflict markers are never persisted to a user artifact.

## 12. Permissions

Initial policy:

| Operation | Owner | Member with write access | Viewer |
|---|---:|---:|---:|
| View graph and versions | Yes | Yes | Yes |
| Compare versions | Yes | Yes | Yes |
| Create a branch | Yes | Yes | No |
| Edit an existing branch | Yes | Yes | No |
| Apply changes to a writable branch | Yes | Yes | No |
| Merge into Main | Yes | No by default | No |
| Archive or delete a branch | Yes | No by default | No |
| Restore Main | Yes | No by default | No |

The owner may later delegate a “maintain Main” capability. Branch protection is an
application authorization rule, not only a Git configuration.

## 13. Data and service model

### 13.1 Branch identity

Introduce a stable `WorkspaceBranch` record with at least:

- `id`;
- `workspace_id`;
- `name` and optional purpose;
- safe internal Git ref;
- base commit SHA;
- cached tip SHA for display, always verified before writes;
- status: active, merged, archived, or unavailable;
- created/resolved timestamps and actors;
- source branch and source version, when applicable.

Main receives a normal branch record during migration so every branch-aware table
can use the same foreign-key contract.

### 13.2 Branch-aware records

At minimum, add `branch_id` to:

- artifact versions;
- workspace events where the event affects branch state;
- proposed changes;
- Yjs updates and collaborative artifact state;
- section/claim analysis jobs and results;
- coherence, readiness, and expert-review jobs/results;
- locks whose meaning applies to a branch artifact rather than the entire workspace.

Records that already pin a commit retain that commit as the immutable content anchor.
`branch_id` answers which active line and permissions contextualize it; commit SHA
answers which exact content it describes.

### 13.3 Graph read model

The graph API must derive topology from Git parent relationships and enrich nodes with
database provenance. It returns stable node and edge identifiers, branch-tip labels,
changed-artifact summaries, authorship, timestamps, milestones, and live-tail state.

Git is canonical for commit topology. The database is canonical for Grant Lab actors,
semantic causes, permissions, and derived-state links. Reconciliation must tolerate a
commit that exists before its metadata row.

The graph is paginated by topology-aware windows rather than naïve timestamp pages so
visible parent and merge relationships are not severed without continuation markers.

## 14. Worktree architecture

Worktrees are an internal isolation mechanism and are not durable product objects.

- Each active branch is associated with at most one server-managed checked-out
  worktree per application service instance.
- Worktree directory names use opaque branch IDs, never user labels.
- A worktree may be created lazily and retired when inactive; the Git branch remains
  durable.
- Opening a branch verifies that the worktree is attached to its expected ref and
  commit before reading or writing.
- All filesystem paths are resolved beneath a configured worktree root.
- Worktree removal never follows links and never proceeds while live edits, a commit,
  or a merge operation are active.

Concurrency uses two levels:

1. a repository/ref mutex for creating, moving, merging, archiving, or removing refs
   and worktrees;
2. a branch commit mutex for ordinary saves, checkpoints, citation mutations, and
   accepted proposals.

This permits useful parallel work across branches without allowing two operations to
race on the same branch or on repository-level metadata.

The initial implementation may retain the current workspace-wide commit mutex for
safety. Splitting it is a performance improvement only after branch isolation tests
prove the stronger invariants.

## 15. Collaborative editing invariants

1. A collaboration room is keyed by workspace, branch, and artifact.
2. Hydration reads only that branch's Git tip and durable update log.
3. A checkpoint advances only that branch/artifact watermark.
4. Switching branches disconnects from the old room before joining the new one.
5. Presence never appears across branches unless a separate overview explicitly
   summarizes it.
6. Creating a branch or merge preview requires a stable checkpoint boundary.
7. A completed merge invalidates the destination room's old canonical state and makes
   connected clients reload through a controlled handover.
8. Source-room edits made after a merge preview do not enter the merge silently; they
   stale the preview.

## 16. Assistant context and behavior

Assistant context always names the active branch:

```text
Application: Digital Pathology
Branch: Ambitious aims
Artifact: Application
Section: Expected impact
```

Assistant responses that refer to “the current draft” mean the current branch. Links
to content include branch and commit identity so opening a result cannot resolve to a
same-named section on the wrong branch.

The assistant may:

- explain graph history;
- compare versions or branches;
- recommend creating a branch;
- prepare changes inside the current branch;
- propose semantic conflict resolutions;
- summarize the consequences of a merge or restore.

The assistant may not:

- create an unrequested branch silently;
- switch the user's active branch while they are typing;
- merge into Main without the protected confirmation path;
- resolve a merge conflict invisibly;
- delete or rewrite branch history;
- present stale review results as current after a merge.

## 17. Notifications and activity

Activity language uses application concepts:

- “Anna created branch ‘Ambitious aims’ from Main.”
- “Luis saved a version in ‘Ambitious aims’.”
- “Assistant proposed changes to Methodology in ‘Short form’.”
- “Anna merged ‘Ambitious aims’ into Main.”
- “Readiness results on Main are stale after the merge.”
- “Luis restored Expected impact from version … as a new version.”

Every event links to the relevant graph node, branch, artifact, or merge report.

## 18. Empty, loading, and failure states

### No branches

The graph still shows Main's linear history and teaches the feature gently:

> Explore an alternative without changing Main. Create a branch from any version.

### Graph loading

The table skeleton preserves column widths. Previously loaded topology remains visible
while older pages load. Filters never briefly reconnect unrelated lanes.

### Missing or unhealthy worktree

The branch remains safe. The UI reports that the editable copy is being rebuilt and
does not expose raw filesystem errors. Authorized technical details show worktree and
ref diagnostics.

### Merge conflict

A conflict is a normal review state, not a fatal error. The merge remains a preview
until all blocking items are resolved.

### Reconciliation

A reconciled commit appears in the graph with an explanatory technical status. The
normal title should be reconstructed from its Git message when semantic provenance is
unavailable.

## 19. Performance and scale

- Virtualize graph rows and details for long histories.
- Load the newest connected topology window first.
- Cache changed-artifact and section summaries by commit.
- Compute expensive semantic diffs on demand and cache by `(base, source, target)`.
- Keep graph layout deterministic so lanes do not jump when metadata arrives.
- Limit live awareness and editor hydration to the active branch; the graph does not
  open collaboration rooms for every visible branch.
- Archive stale experiments without removing their topology.

## 20. Delivery slices

### Slice 1 — Useful linear history

- Replace the current message/creator list with the interactive graph/table surface.
- Show Main, immutable versions, live changes, authors, timestamps, changed artifacts,
  and semantic causes.
- Add read-only historical viewing and two-version comparison.
- Add technical details and the accessible List view.

### Slice 2 — Forward recovery

- Add full and selective restore as new versions.
- Add Apply from… for sections within Main's history.
- Add stale-result invalidation and citation validation.

### Slice 3 — Branch isolation

- Migrate Main into the branch data model.
- Add branch-aware persistence, collaboration, proposals, and analysis.
- Add managed worktree lifecycle.
- Add create, switch, rename, and archive branch operations.
- Render divergent lanes in the graph.

### Slice 4 — Cross-branch comparison and transfer

- Compare branch with Main or another branch.
- Apply selected prose, requirements, and citations.
- Add assistant-supported comparison summaries.

### Slice 5 — Merge

- Add three-way section classification and merge previews.
- Add interactive conflict resolution.
- Add cross-artifact validation and protected Main finalization.
- Create and render two-parent merge commits.
- Add merge reports, invalidation, and recovery tests.

Each slice requires a separate implementation plan, migration strategy, rollback plan,
and end-to-end test matrix for collaborative and non-collaborative editing.

## 21. Acceptance criteria

The feature direction is complete when:

1. A user can explain the relationship between Main, a branch, a version, and live
   changes without knowing Git.
2. The graph accurately renders linear history, divergence, branch tips, restore
   commits, and merge commits.
3. Every graph operation clearly names its source and destination.
4. A user can open and compare a historical version without changing current work.
5. Restore never removes later history.
6. Two branches can be edited concurrently without sharing Yjs updates, proposals,
   checkpoint watermarks, derived results, or worktree files.
7. A stale proposal from one branch cannot be accepted into another.
8. A selected prose change carries valid citation data or is blocked with an
   actionable explanation.
9. A merge preview becomes stale if either tip changes.
10. Main cannot receive a merge without the required human permission and
    confirmation.
11. The normal merge UI never shows or commits raw conflict markers.
12. A successful merge creates a two-parent commit, a provenance report, and visible
    graph edge.
13. Derived results affected by restore, transfer, or merge are marked stale or
    re-enqueued against the resulting commit.
14. All graph and version operations are usable by keyboard and screen reader through
    an equivalent List view.
15. Worktree recreation or service restart does not change branch history or lose
    durable collaborative edits.

## 22. Explicit non-goals for the first release

- Exposing an unrestricted Git terminal or arbitrary ref manipulation.
- Remote Git hosting, pull requests, pushes, or forks between Grant Lab deployments.
- Force-push, rebase, amend, squash, or interactive history editing.
- Drag-and-drop branch merging.
- Branch-specific workspace membership.
- Automatic assistant merges into Main.
- Merging binary uploaded source files.
- Treating every autosync update as a graph commit.

These exclusions preserve a comprehensible, recoverable application workflow while
retaining Git's useful topology underneath.
