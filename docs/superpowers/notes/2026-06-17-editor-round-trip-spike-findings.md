# Editor round-trip spike — findings & decision input (2026-06-17)

**Spike branch:** `spike/editor-round-trip` (worktree, off master). Harness at `frontend/src/spike/`.
**Gate:** spec §12 — property-test a citation `DecoratorNode` + idempotent markdown serialization
against the `split_sections` join==doc invariant BEFORE committing the SP2a editor framework.
Decision rule: Lexical → (if fragile) ProseMirror → (if still fragile) CodeMirror-6 source.

## Outcome: **GATE NOT CLEARED** for naive Lexical + `@lexical/markdown`

A first pass built a Lexical headless harness (TS port of `split_sections`, a `CitationNode`
`DecoratorNode`, markdown import/export, fast-check property tests P1–P4) and reported
**LEXICAL_HOLDS** at 2000 runs + a differential check vs real Python. A 5-agent adversarial
verification (4 attack lenses + judge) **overturned it**: the green suite was an artifact of a
generator whose alphabet was `[A-Za-z0-9]`, so it could never emit the characters
(`_ \ ~ #`, escapes, emphasis) that actually break `@lexical/markdown`'s serializer. Every break
below was reproduced with a concrete input and **cross-checked byte-for-byte against the real
Python `split_sections`** (the TS port is trustworthy — 7/7 differential).

### Confirmed fundamental fragilities (Lexical + `@lexical/markdown` defaults)

1. **Snake_case heading escaping (identity mutation).** `## Aims_and_Scope` → `## Aims\_and\_Scope`.
   Python `split_sections` heading goes `Aims_and_Scope` → `Aims\_and\_Scope`. The section heading
   string — its identity — is mutated. snake_case headings (`## Work_Package_1`,
   `## Aims_and_Objectives`) are ubiquitous in grant writing. The gate's own P2 fails on this.
2. **Escaped-heading phantom promotion.** `\## escaped` → real `## escaped` boundary. Section count
   2→3 (Python-confirmed); a following citation marker is silently re-attributed to the phantom
   section. P2 fails.
3. **Backslash doubling/dropping.** `## \escaped` → `## \\escaped`; body `\alpha` → `\\alpha`,
   `C:\Users\…` → `C:\\Users\\…`, `\sum_{i}` → `\\sum\_{i}`, `\$` → `$`. LaTeX math, Windows paths,
   gene names — corrupted on first save with no P1/P2/P3 signal.
4. **Tilde-fence / indented-code marker PROMOTION.** A literal `[@fake]<!-- ev:… -->` inside a `~~~`
   fence or a 4-space-indented code block imports as a **real `CitationNode`** (observed 2 nodes vs
   the backtick-fence control's 1) — a fake citation enters the citation-audit/select-to-cite
   machinery; the `~~~` fence is corrupted to `\~\~\~`. `@lexical/markdown`'s base TRANSFORMERS only
   honor ` ``` ` fences. P3 (a byte-regex) is structurally blind to it.
5. **Section-SHA churn on first import (the load-bearing one).** On the exact shape the backend
   writes (`## Background\nOld text.\n## Aims\n…`), the first import→export canonicalizes **every**
   section's bytes (blank line inserted after each heading/block; file-final `\n` stripped):
   3/3 sections change `sha256` (Python-confirmed `c4f0511f… → 063bce62…`). The backend keys ALL
   anti-clobber/staleness on byte-exact `sha256(section.text)` (`sectionizer.py Section.sha`;
   `proposing.capture_base`; `staleness.refresh_staleness` compares `live_sha != base_section_sha`),
   and short-circuits no-op saves only on byte-exact `new_text == live.text` (`save.py`). So the
   first save of any existing `proposal.md` would: invalidate **every** pending agent proposal
   (mass false-stale), emit a spurious git commit per section, and re-enqueue Tier-2 claim analysis.

### The cross-cutting truth

Breaks 1–4 are **Lexical-serializer-specific** (the `@lexical/markdown` escape set is per-character
configurable; transformers are extensible — "tunable, not fundamental"). **Break 5 is architectural
and applies to ANY WYSIWYG-markdown editor, including ProseMirror:** a rich editor canonicalizes on
import, and our staleness backbone demands byte-exact section bytes. Whichever editor we pick, the
git↔editor byte agreement must be reconciled.

## Decision options for the SP2a editor

- **(A) Harden Lexical.** Custom byte-faithful serializer (bypass `@lexical/markdown` escaping; route
  `HeadingNode` text + `CitationNode` as structured nodes that skip text-escaping) + write-time
  canonicalization at every backend write path + key staleness on a normalized section form. Most
  engineering; preserves full WYSIWYG. Re-run the **hardened** property suite (below) before trusting.
- **(B) Fall back to ProseMirror (§12 order).** Different escaping specifics, but **break 5 is
  identical** and the write-time-canonicalization + staleness re-architecture is still required.
  Needs its own spike.
- **(C) CodeMirror-6 source + atomic citation widgets.** Edit the **raw `proposal.md` bytes**;
  render `[@key]<!-- ev:… -->` markers as atomic inline widgets via CM6 `Decoration.replace`. **No
  serialization round-trip → section bytes are stable by construction → the staleness backbone is
  untouched (zero risk to SP2a-1/SP2a-2 work).** Trades full-WYSIWYG for "rich markdown source +
  citation pills." Lowest risk; arguably fine/preferred for a technical grant-writing audience.

## Required follow-ups (whichever path) — from the adversaries

1. **Byte-faithful headings/prose:** no backslash-escaping on export; `## Aims_and_Scope`, `## \escaped`,
   `\alpha`, Windows paths must round-trip **byte-exact**, not merely idempotently.
2. **CommonMark escapes:** `\##` must not become a real heading (no phantom sections); leading `\` in
   heading text must not double.
3. **Write-time canonicalization** through the same serializer at every backend write path
   (Phase-0 scaffold, `tools/proposal.py`, `save_section`, `insert_from_annotation`) so git HEAD and
   editor output agree; reconcile the trailing-newline conflict (editor strips file-final `\n`;
   `save_section` step-5 adds section-trailing `\n`) — one policy, or compare SHAs on a normalized form.
4. **One-time migration:** every already-committed (terse-shape) `proposal.md` drifts 100% of section
   SHAs on its first WYSIWYG save — canonicalize all repos up front, or compare staleness on a
   normalized form so the first-open mass-staleness event is invisible.
5. **Code-block marker contract:** decide/support `~~~` + indented code, or forbid/escape
   marker-shaped strings outside backtick fences; assert via **node-tree inspection** (not byte-regex)
   that no phantom `CitationNode` is minted.
6. **Strengthen the property suite before re-running any gate:** assert byte-exact (or sha-stable)
   **first-pass** round-trip (`md === roundtrip(md)`), assert **prose-body** fidelity (emphasis,
   code-fence integrity, raw backslashes), and broaden the generator alphabet beyond `[A-Za-z0-9]` to
   include `_ * \` [ ] # > \ ~` and CRLF. (Option C sidesteps most of this — there is no round-trip.)

## Recommendation

The byte-exact section-SHA staleness keying is the load-bearing invariant of the entire propose/accept
backbone just hardened across SP2a-1/SP2a-2. Both WYSIWYG options (A, B) fundamentally fight it
(break 5) and require a staleness re-architecture before they're safe. **Lead recommendation: (C)
CodeMirror-6 source + atomic citation widgets** — it preserves the backbone with zero round-trip risk
and is plausibly the right UX for this audience. If full WYSIWYG is a hard product requirement, **(A)
hardened Lexical** is viable but is materially more work and carries the staleness re-architecture as a
prerequisite. This is a product/UX-vs-engineering call for the user.

**Spike artifacts kept** on `spike/editor-round-trip` (harness + the adversarial property cases are
reusable for whichever path; the hardened test requirements above are the spec for re-running a gate).
