# Grant Lab — Assistant-Led IDE Product Direction

**Date:** 2026-08-15  
**Status:** Product and interaction north-star; follow-up implementation specs required.  
**Scope:** The Grant Lab application shell, opportunity reading experience, proposal cockpit, and the interaction contracts connecting their parts.

## 1. Decision

Grant Lab remains an IDE.

The proposal process contains several durable, interdependent objects: the funding
opportunity, extracted requirements, the application draft, claims, sources,
citations, proposed changes, evaluations, review results, collaborators, and version
history. A linear wizard or document-only interface would make those relationships
harder to inspect and would reduce the control needed for consequential grant work.

The product direction is therefore an **assistant-led IDE**:

- the full workbench and its underlying objects remain available;
- the default layout is calm and task-focused;
- capabilities appear when they become relevant instead of all competing for
  attention immediately;
- the assistant can explain, navigate, and orchestrate the workbench;
- consequential assistant actions remain proposed, inspectable, attributable,
  reversible, and under human control.

The governing principle is **progressive disclosure without capability removal**.

This document defines the product model and interaction rules. It does not replace
the smaller feature specs needed to implement individual slices.

## 2. Product thesis

Grant Lab should feel like a knowledgeable grant collaborator operating inside a
professional workbench.

The IDE provides spatial stability, visibility, and control. The assistant reduces
the effort of understanding dependencies and navigating between them. Neither is a
secondary feature: the product is the combination.

The intended experience is:

> I can see every important part of the application, understand how the parts affect
> each other, ask for help in natural language, inspect what the assistant did, and
> take direct control at any time.

The assistant must not become a chat-shaped alternative to the IDE. It should operate
the same objects and navigation model that the user operates manually.

## 3. Goals and non-goals

### 3.1 Goals

1. Preserve efficient access to every proposal artifact and diagnostic surface.
2. Make relationships between artifacts directly traversable in both directions.
3. Give new or infrequent users a useful default without creating a separate,
   diminished product mode.
4. Let expert users arrange, pin, search, and navigate the workbench efficiently.
5. Make the assistant aware of the active workspace, artifact, section, selection,
   sources, and relevant requirements.
6. Keep AI-generated changes reviewable and preserve provenance, history, and undo.
7. Make background work, failures, stale results, locks, permissions, and save state
   visible at the point where they matter.
8. Use consistent terminology and consistent severity/readiness semantics across all
   surfaces.

### 3.2 Non-goals

1. Do not replace the cockpit with a wizard or a conventional sequence of forms.
2. Do not create separate “simple” and “advanced” products with different data or
   capabilities.
3. Do not make chat the only way to access an operation.
4. Do not let the assistant silently edit, accept, lock, delete, submit, or otherwise
   perform consequential actions.
5. Do not hide technical or audit information permanently. It may be placed behind an
   inspector or expander when it is not part of the normal decision.
6. Do not force a proposal through phases in a strictly linear order. Recommended next
   steps are guidance, not navigation locks.

## 4. Primary users and jobs

### Researcher or principal investigator

- Determine whether an opportunity is worth pursuing.
- Understand the funder's requirements and eligibility constraints.
- Produce a persuasive, compliant application without losing scientific ownership.
- Know what is weak, unsupported, incomplete, or over the limit.

### Grant coordinator or research administrator

- Monitor opportunities and several applications.
- Confirm that requirements have been interpreted correctly.
- Review readiness, evidence, deadlines, ownership, and unresolved problems.
- Move efficiently between applications without losing context.

### Proposal collaborator or reviewer

- Work on an assigned part of the application.
- Comment or propose changes without accidentally overriding the owner's decisions.
- See the requirements, sources, and review context relevant to the current section.

These users differ in frequency and depth, but should use the same workbench. Layout,
permissions, and disclosure adapt; the object model does not.

## 5. The connected object model

The most important UX requirement is not the visual styling of any individual pane.
It is that related objects behave as one connected system.

```text
Funding opportunity
        │
        ▼
Funder requirement ⇄ Application specification
        │                         │
        └───────────┬─────────────┘
                    ▼
              Draft section
               │    │    │
               │    │    └──── Proposed change ⇄ Assistant turn
               │    └───────── Claim ⇄ Citation ⇄ Source passage
               └────────────── Readiness issue ⇄ Review finding
                                      │
                                      ▼
                              Activity and versions
```

Every relationship above must be traversable:

- A requirement shows which specification decision and draft sections address it.
- A section shows its requirements, problems, claims, citations, and pending changes.
- A problem opens and highlights the affected content.
- A citation opens the exact supporting passage and its metadata.
- A source shows every place where it is cited.
- An assistant proposal identifies the affected artifact and section before review.
- A readiness or review result links to the evidence that produced it.
- An activity or version entry links back to the affected object where possible.

The user should not have to remember which panel contains the related object.

## 6. Information architecture

The global destinations are organized around durable user objects:

| Destination | Primary object | Purpose |
|---|---|---|
| Opportunities | Funding opportunities | Discover, evaluate, save, dismiss, share, and start an application. |
| Applications | Proposal workspaces | Manage active, submitted, awarded, rejected, or abandoned applications. |
| Research profiles | Matching context | Describe who is seeking funding and tune opportunity matching. |
| Settings | Personal preferences | Account, notifications, digest subscriptions, and display preferences. |
| Administration | Operational state | Users, sources, ranking, drafting, and system health. |

“Calls” may remain an accepted domain term in source data and technical detail, but
“Opportunities” better describes the user's destination. “Workspace” remains the
technical entity; “Application” is the user-facing object.

## 7. IDE anatomy

The stable application cockpit uses the following layout:

```text
┌──────────────────────────────── Application header ────────────────────────────────┐
├──────┬────────────────┬──────────────────────────────────────┬─────────────────────┤
│Global│ Navigator      │ Artifact bar                         │ Assistant           │
│nav   │                ├──────────────────────────────────────┤                     │
│      │ Outline        │                                      │ Context             │
│      │ Requirements   │ Main working surface                 │ Conversation        │
│      │ Sources        │                                      │ Actions / status    │
│      │ History        │                                      │                     │
│      │ Members        │                                      │                     │
├──────┴────────────────┴──────────────── Workbench panel ─────┴─────────────────────┤
└────────────────────────────────── Status bar ───────────────────────────────────────┘
```

Users may resize or collapse regions. The same data and commands remain available
regardless of layout.

### 7.1 Global activity bar

The activity bar remains compact for expert navigation, with an expandable labeled
state for discovery.

Requirements:

- Icons have persistent accessible names and tooltips.
- A user can expand the bar to show destination labels.
- The current application is visually distinct from the Applications list.
- Recent applications are reachable without first returning to the full list.
- A command palette provides keyboard access to destinations and commands.
- Clicking an already-active destination may toggle its navigator, but this behavior
  must be taught through a tooltip and must not discard URL state.

### 7.2 Application header

The application header answers: “What am I working on, what state is it in, and what
can I do globally?”

It contains:

- application title and linked funder/opportunity;
- current branch and access to branch switching, comparison, and creation;
- application lifecycle status;
- deadline and relative time remaining;
- current readiness state and stale indicator;
- collaborator presence or member count;
- Share, Export, layout, and workspace-menu actions.

The signed-in identity and sign-out command move into an account menu rather than
competing with application controls.

Header state is actionable:

- deadline opens deadline/application settings;
- readiness opens the relevant readiness view;
- stale state explains what changed and offers a re-run;
- collaborators open sharing and permissions;
- linked opportunity opens its detail without losing the application context.

### 7.3 Left navigator

The navigator is a contextual map, not only a collection of object lists.

The default group is the active artifact's outline. Each section may show compact
signals for:

- unsaved or uncheckpointed work;
- pending proposed changes;
- errors and warnings;
- citation or claim coverage;
- word/character usage against a funder limit;
- linked requirements;
- collaborative presence.

Example:

```text
APPLICATION
  ✓ Executive summary          485 / 500
  ⚠ Methodology                2 issues
  ● Implementation             1 suggestion
  ○ Budget justification       no evidence

REQUIREMENTS
  ✓ Clinical relevance
  ⚠ Implementation plan
  ○ Dissemination
```

References, versions, and members remain available as collapsible groups. Empty groups
start collapsed and explain how they become populated. The navigator supports search
across sections, requirements, sources, citations, and history.

Selecting an object updates the other panes' context. It does not automatically run an
AI operation or discard an editor selection.

### 7.4 Artifact bar

The existing artifact separation remains because each artifact has a distinct purpose.
Labels should combine precision with an explanation:

| Artifact | User-facing label | Meaning |
|---|---|---|
| Draft | Application | The text that will be submitted. |
| Spec | Requirements | The agreed interpretation and plan for satisfying the call. |
| Scorecard | Readiness | A criterion-by-criterion assessment of the current application. |
| Panel review | Expert review | A deeper multi-perspective critique. |

The artifact bar shows state without becoming a restrictive stepper:

```text
Application    Requirements ✓    Readiness · 2 issues    Expert review —
```

It may recommend a next action, for example:

- Requirements changed — review the affected sections.
- Application changed — readiness result is stale.
- Two readiness gaps remain — open the most important gap.
- Readiness is current — expert review is available.

Users remain free to open any artifact at any time, subject only to real permission or
lock constraints.

### 7.5 Main working surface

The central surface is selected by the active artifact and is always the primary place
for consequential work.

For the application draft:

- WYSIWYG is the default authoring surface.
- Section and full-document views remain available.
- Headings, lists, tables, citations, and comments are rendered as document objects.
- Word or character limits are visible per section and for the full application.
- Requirements, problems, and suggestions can be shown as inline or margin indicators.
- Citation pills open their exact source passage.
- Direct human edits and assistant-proposed edits have visibly different states.
- Save, sync, checkpoint, conflict, and read-only states use consistent language.
- Markdown/source editing remains available as an explicit technical view.

For Requirements, Readiness, and Expert review, the central surface retains the same
selection and deep-link contract. A finding can open the relevant application section
without forcing the user to reconstruct the connection manually.

### 7.6 Assistant pane

The assistant pane shows its scope before the conversation:

```text
CONTEXT
Application: Digital Pathology
Artifact: Application
Section: Methodology
Selection: 124 words
Included sources: 2
```

The context can follow the workbench or be pinned. A pinned context is visually
obvious, preventing accidental scope changes while the user navigates elsewhere.

The assistant offers contextually useful commands such as:

- Explain this requirement.
- Check this section against its requirements.
- Find evidence for the selected claim.
- Shorten this section to its limit.
- Show the weakest readiness criterion.
- Compare this section with the previous version.
- Prepare changes that address these warnings.

The assistant can navigate the IDE in response to an explicit command or clicked result:
open an artifact, select a section, reveal a source passage, or open a workbench tab. It
must not silently rearrange the user's layout while a turn is running.

Tool activity is summarized in normal conversation and expandable for inspection. Raw
tool chatter is not the default transcript.

### 7.7 Bottom workbench panel

The bottom panel holds secondary, inspectable work. It starts collapsed unless there is
pending attention or the user previously left it open.

Its information is grouped without removing underlying capabilities:

| Group | Contents |
|---|---|
| Changes | Assistant and human proposed changes; accept/reject review. |
| Checks | Problems, claim analysis, citation audit, and coherence findings. |
| Sources | Reference library, files, citations, metadata, and source passages. |
| History | Activity, versions, comparisons, and restore actions. |

History expands into a commit graph that shows Main, named branches, checkpoints,
live-but-uncheckpointed changes, restores, and merges. The graph is an interactive
control surface: users can open a historical version, compare, create a branch,
apply selected changes, restore forward, or begin a protected merge. The detailed
interaction and isolation contracts are defined in
[Branches, Merging, and History Graph](2026-08-15-branches-merging-history-graph-design.md).

The panel may open contextually when:

- a proposed change arrives;
- a check completes with a problem;
- the user follows a citation or source link;
- a background operation fails;
- the user follows a deep link from the assistant or readiness view.

Auto-opening never steals keyboard focus from active typing. Counts encode severity
consistently: errors, warnings, proposed changes, and neutral totals do not share an
indistinguishable badge.

### 7.8 Status bar

The status bar contains compact, persistent state rather than navigation labels or
decorative metadata.

Potential items:

- saved/saving/unsaved/sync/offline state;
- deadline state;
- requirements lock state;
- current readiness and stale state;
- collaboration connection and read-only state;
- word/character count in document mode;
- line and column only in Markdown/source mode.

Every item that represents a condition is actionable:

- “2 issues” opens Checks filtered to the current scope;
- “Requirements locked” opens lock details;
- “Saved” opens recent history or checkpoint information;
- “Offline” opens connection and recovery information;
- deadline opens the application deadline settings.

## 8. Cross-pane interaction laws

These laws apply across the workbench and should be tested as product invariants.

1. **One selection, shared context.** The active application, artifact, section, and
   selected object form a common context consumed by navigator, editor, assistant, and
   workbench panel.
2. **Every count is a link.** A count or status badge opens the rows that produced it,
   already filtered to the relevant scope.
3. **Every finding has a destination.** Problems, readiness criteria, review findings,
   citations, and proposed changes deep-link to the exact affected object.
4. **Navigation never implies acceptance.** Opening a proposed change, source, or
   assistant result does not apply or approve it.
5. **Context changes do not discard work.** Pane navigation respects dirty-state guards,
   collaborative persistence, and browser back/forward behavior.
6. **State has one meaning everywhere.** Match bands, readiness bands, severities, stale
   state, and job state use a single vocabulary and threshold source.
7. **Background work remains visible.** A running operation exposes progress or a clear
   activity state; completion, failure, and cancellation are surfaced where the user
   initiated it.
8. **Empty panes teach their purpose.** An empty state says what belongs there and gives
   the most relevant action to populate it.
9. **Layout is user state.** Widths, collapsed groups, open workbench group, and layout
   preset persist per user without changing the underlying application.
10. **Keyboard and pointer reach the same operations.** The command palette, shortcuts,
    focus order, and accessible labels cover all primary workbench actions.

## 9. Assistant control contract

The assistant earns trust through predictable control boundaries.

### 9.1 Read-only actions

Explanation, navigation, search, comparison, and analysis may execute directly. The
assistant reports the context and sources used.

### 9.2 Proposed document changes

Edits to the application or requirements become proposed changes unless the user is in
an explicitly chosen direct-edit interaction. A proposal includes:

- target artifact and section;
- concise intent;
- redline or before/after comparison;
- requirements, problems, or user instruction it addresses;
- sources or citations involved;
- assistant/model provenance;
- accept, reject, and revise controls.

Applying multiple changes requires an explicit grouped approval with the affected
sections listed.

### 9.3 Consequential operations

Locking requirements, changing permissions, deleting data, restoring a version,
changing lifecycle status, exporting a submission artifact, and any future submission
operation require an explicit UI confirmation. A conversational “yes” alone must not
bypass the normal confirmation and authorization path.

### 9.4 Provenance and recovery

Accepted changes enter activity and version history. The user can inspect who or what
made a change, compare it, and restore a previous state as a new version rather than
rewriting history.

## 10. Layout presets

Grant Lab uses layout presets rather than separate beginner and expert modes. A preset
only arranges or collapses panes; it never changes available data, permission, or
behavior.

| Preset | Default emphasis |
|---|---|
| Writing | Navigator outline, application editor, assistant; workbench collapsed. |
| Requirements | Requirements navigator and surface, linked opportunity/source, assistant. |
| Sources | Application or claim context, source library/passage viewer, citations. |
| Review | Readiness or expert review, affected application section, Checks/Changes. |
| Full workbench | All persistent regions visible for maximum oversight. |

Users can customize any preset and save the resulting arrangement. The assistant may
offer “Open review layout,” but does not switch layouts silently.

## 11. Key workflows

### 11.1 Opening an active application

The default Writing layout opens with the last active section, the outline, and the
assistant. Empty navigator groups and the workbench panel are collapsed. The header
shows deadline, lifecycle status, readiness, and collaborators. If something requires
attention, one concise next-action banner identifies the highest-value action.

### 11.2 Following a readiness problem

Selecting a criterion in Readiness opens its explanation, linked requirements, and
affected sections. “Go to section” opens the application at the exact section and
reveals Checks filtered to that issue. The assistant context follows and offers to
explain or prepare a proposed fix.

### 11.3 Reviewing an assistant change

When a turn produces edits, Changes opens without taking focus. The assistant message
summarizes the target sections. Selecting a proposal opens the affected section and
redline together. Accept, reject, and request-revision actions are available without
leaving the application context.

### 11.4 Following a citation

Selecting a citation opens Sources at the exact quoted passage, shows metadata and
verification state, and highlights every application location using the source. Closing
the source view returns focus to the original citation.

### 11.5 Initial requirements review

Workspace setup is a workbench task, not an isolated technical table. The owner can
compare the extracted requirement with the source passage that produced it, see
confidence or uncertainty, edit the interpretation, and ask the assistant for an
explanation. A primary “Confirm requirements and begin” action is available; relative
weights and scale maxima remain under advanced evaluation settings.

## 12. Opportunity-to-application boundary

The IDE begins before drafting, but discovery should not reserve every cockpit pane.

- Opportunities use a list plus an adaptive reading pane.
- The reading pane stays collapsed until an opportunity is selected and becomes wide
  enough for decision content when opened.
- The opportunity detail emphasizes fit, eligibility, deadline, budget, source, and the
  reasons for the recommendation.
- Save, dismiss, share, and start-application are distinct decisions.
- Starting an application carries forward the selected research profile, opportunity,
  deadline, and known metadata; users are not asked to re-enter information the system
  already has.
- Once created, the Application opens in setup or the normal cockpit using the same
  assistant and cross-object navigation model.

## 13. Terminology

Terminology should describe user intent while preserving technical precision where it
helps.

| Current/internal term | Preferred primary label | Where the internal term remains useful |
|---|---|---|
| Call | Opportunity | Source metadata and technical inspection. |
| Workspace | Application | API, logs, and administration. |
| Profile | Research profile / Funding interests | Data model and administration. |
| Draft | Application | Artifact inspector and version metadata. |
| Spec | Requirements | Artifact inspector and lock details. |
| Scorecard | Readiness | Technical export and evaluator details. |
| Panel review | Expert review | Run details and administration. |
| Inbox | Changes | The proposed-change subtype may retain “inbox.” |
| Problems | Checks | Individual errors and warnings retain precise types. |
| Library | Sources | Technical reference metadata. |
| Aggregate score | Match | Technical ranking details. |
| Bootstrap profile | Create research profile | Job state and administration. |

Model names, cosine similarity, prompt/extractor versions, reason codes, and raw job
state remain available in technical details rather than leading normal user decisions.

## 14. States, accessibility, and responsive behavior

### States

Every region defines loading, empty, permission-denied/read-only, offline, stale,
failed, and deleted/not-found behavior. Errors include a safe recovery action such as
retry, reopen, restore context, or contact an administrator. Raw internal errors are
available for authorized inspection but do not replace user-oriented guidance.

### Accessibility

- Panes and resize handles have named landmarks and controls.
- Collapsing a pane returns focus predictably and reopening restores a sensible focus.
- Status is never communicated by color alone.
- The command palette and shortcuts have visible discoverability.
- Dense tables provide a readable stacked alternative at constrained widths.
- Motion from contextual pane opening respects reduced-motion preferences and never
  steals focus from typing.
- Both dark and light/system color schemes should be supported for long-form reading.

### Responsive behavior

The cockpit is desktop-first, but must remain usable on common hospital laptops.

- At narrower desktop widths, the assistant becomes a toggleable overlay or drawer.
- The navigator may collapse to a labeled icon rail.
- The bottom panel becomes a full-width drawer.
- The central working surface retains a minimum readable line length and never shrinks
  into a narrow residual column.
- Layout degradation follows a documented priority rather than relying only on manual
  resize handles.

## 15. Delivery strategy

This north-star should be implemented as independently valuable slices.

### Slice 1 — Semantic consistency and actionable chrome

- Unify score/readiness/severity terminology and thresholds.
- Make header and status items actionable.
- Add user-facing labels to global navigation and account-menu cleanup.
- Establish shared deep-link/context types for section, criterion, problem, source, and
  proposed change.

### Slice 2 — Connected navigator and workbench panel

- Add section-level status indicators.
- Consolidate bottom-panel information into Changes, Checks, Sources, and History.
- Make every count and finding deep-link to exact content.
- Improve empty, running, failed, and stale states.

### Slice 3 — Contextual assistant

- Show and pin assistant scope.
- Add assistant-initiated navigation through explicit results/actions.
- Summarize tool activity with expandable technical detail.
- Standardize proposed-change summaries and grouped approval.

### Slice 4 — Writing and layout foundation

- Make the collaborative WYSIWYG editor the normal surface.
- Add formatting, find, counts/limits, document structure, and focus behavior.
- Add layout presets and persist user arrangement.
- Add human-facing version comparison and restore.

### Slice 5 — Requirements and evidence integration

- Redesign setup as source-linked requirements review.
- Add requirement-to-section coverage links.
- Add claim/citation/source reverse navigation.
- Connect readiness and expert review findings to the same relationship model.

### Slice 6 — Submission-quality output

- DOCX and PDF export.
- Template and section-limit enforcement.
- Export readiness summary and blocking checks.
- Preserve citation, bibliography, table, and formatting fidelity.

### Slice 7 — Branches and history graph

- Replace the linear Versions list with an accessible commit graph and version details.
- Add read-only historical viewing, comparison, and forward-only restore.
- Add named branches backed by isolated worktrees.
- Add selective cross-branch transfer and section-aware, evidence-aware merging.
- Keep assistant proposals, collaboration, checks, and provenance branch-scoped.

Each slice requires its own design/implementation spec and testable acceptance criteria.

## 16. Success measures

The design succeeds when users can complete important cross-object tasks without being
taught where each panel lives.

Suggested usability measures:

- Time for a new user to locate the section responsible for a readiness warning.
- Percentage of users who can trace a citation to its exact source passage.
- Time to review and accept/reject a multi-section assistant proposal.
- Number of navigation actions required to move from a requirement to its draft
  coverage and supporting evidence.
- Percentage of active users who use direct IDE navigation as well as assistant
  navigation.
- Frequency of discarded work or context loss during pane and artifact changes.
- User-reported confidence that AI changes are inspectable and reversible.
- Successful export rate without subsequent manual reconstruction of citations or
  formatting.

A useful qualitative test is:

> Can a researcher explain what is currently weak, why it is weak, where to fix it,
> what evidence supports it, and what the assistant proposes—without leaving the
> workspace or asking an administrator?

## 17. Related design documents

- [Funding Calls — Outlook-style Reading Pane](2026-06-29-calls-reading-pane-design.md)
- [Selection Actions — Canvas-style Ask/Edit/Cite](2026-06-28-selection-actions-design.md)
- [Proposal Template Design](2026-06-28-proposal-template-design.md)
- [Word-processor Feature Backlog](2026-07-07-word-processor-feature-backlog.md)
- [Branches, Merging, and History Graph](2026-08-15-branches-merging-history-graph-design.md)
- [Calls Reading Pane Implementation Plan](../plans/2026-06-29-calls-reading-pane.md)

These documents describe existing components or narrower features. Where a future
slice changes their user-facing behavior, the implementation spec must explicitly
record how it advances this north-star while preserving their correctness and audit
invariants.
