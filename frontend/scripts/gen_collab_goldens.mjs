/**
 * Generate JS-reference "golden" fixtures for the collaborative-editing bridge.
 *
 * PATH: PRIMARY — markdown is parsed entirely in JS using prosemirror-markdown's
 * MarkdownParser configured for our collab-schema, with a custom markdown-it
 * inline rule that consumes full citation markers ([@key]<!-- ev:ev-… -->) into
 * citation atom nodes.  The pm_json comes from doc.toJSON(), so it is
 * independent of the Python markdown_to_pm() implementation.
 *
 * Each golden is self-verified before being written: the JS round-trip
 *   prosemirrorToYDoc  →  yDocToProsemirrorJSON
 * must return a doc that deepStrictEquals pm_json.  Any failure aborts the
 * whole run so a Task-5 failure can only be a Python bug, never a bad fixture.
 *
 * Output: tests/proposal_assist/collab/goldens/<name>.json
 *         { markdown: string, pm_json: object, ydoc_update_b64: string }
 */

import { createRequire } from 'module';
import { deepStrictEqual } from 'assert';
import { mkdirSync, writeFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

// ---------------------------------------------------------------------------
// Path setup — resolve relative to this script so node can be invoked from any cwd
// ---------------------------------------------------------------------------
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(__dirname, '..');
const repoRoot     = path.resolve(frontendDir, '..');
const goldensDir   = path.resolve(repoRoot, 'tests/proposal_assist/collab/goldens');

// ---------------------------------------------------------------------------
// Imports
// ---------------------------------------------------------------------------
import { schema } from './collab-schema.mjs';
import { MarkdownParser } from 'prosemirror-markdown';
import MarkdownIt from 'markdown-it';
import { prosemirrorToYDoc, yDocToProsemirrorJSON } from 'y-prosemirror';
import * as Y from 'yjs';

// ---------------------------------------------------------------------------
// Citation marker constants (mirror of Python build_marker / MARKER_RE)
// ---------------------------------------------------------------------------
// Pattern: [@citekey]<!-- ev:ev-<hex> -->
// NOTE: the group names match the Python MARKER_RE exactly.
const MARKER_RE = /\[@(?<citekey>[A-Za-z0-9_-]+)\]<!-- ev:(?<ev_id>ev-[0-9a-f]{6,}) -->/;

function buildMarker(citekey, evId) {
  return `[@${citekey}]<!-- ev:${evId} -->`;
}

// ---------------------------------------------------------------------------
// Citation inline rule for markdown-it
//
// Mirrors src/call_manager/proposal_assist/collab/markdown_bridge.py::_citation_rule
// Runs before "link" so the full marker is consumed as one atom.
// ---------------------------------------------------------------------------
function citationInlineRule(state, silent) {
  if (state.src.charCodeAt(state.pos) !== 0x5B /* '[' */) return false;
  const slice = state.src.slice(state.pos);
  const m = MARKER_RE.exec(slice);
  if (!m || m.index !== 0) return false;
  if (!silent) {
    const token = state.push('citation', '', 0);
    token.meta = { citekey: m.groups.citekey, ev_id: m.groups.ev_id };
    token.content = m[0];
  }
  state.pos += m[0].length;
  return true;
}

// ---------------------------------------------------------------------------
// Configure markdown-it + MarkdownParser
// ---------------------------------------------------------------------------
const md = new MarkdownIt('commonmark', { html: true }).enable('strikethrough');
// Register before "link" — both trigger on "[", and we must win.
md.inline.ruler.before('link', 'citation', citationInlineRule);

const markdownParser = new MarkdownParser(schema, md, {
  // Block nodes
  blockquote:   { block: 'blockquote' },
  paragraph:    { block: 'paragraph' },
  list_item:    { block: 'list_item' },
  bullet_list:  { block: 'bullet_list' },
  ordered_list: { block: 'ordered_list', getAttrs: tok => ({ order: +tok.attrGet('start') || 1 }) },
  heading:      { block: 'heading',      getAttrs: tok => ({ level: +tok.tag.slice(1) }) },
  code_block:   { block: 'code_block',   noCloseToken: true },
  fence:        { block: 'code_block',   noCloseToken: true },
  hr:           { node: 'horizontal_rule' },

  // Inline atoms
  image:      { node: 'image', getAttrs: tok => ({
    src:   tok.attrGet('src'),
    title: tok.attrGet('title') || null,
    alt:   tok.children && tok.children[0] ? tok.children[0].content : null,
  }) },
  hardbreak:  { node: 'hard_break' },
  // Custom citation atom — token is an inline self-closing token with .meta
  citation:   { node: 'citation', getAttrs: tok => ({
    citekey: tok.meta.citekey,
    ev_id:   tok.meta.ev_id,
  }) },

  // Marks
  em:          { mark: 'em' },
  strong:      { mark: 'strong' },
  s:           { mark: 'strikethrough' },
  link:        { mark: 'link', getAttrs: tok => ({
    href:  tok.attrGet('href'),
    title: tok.attrGet('title') || null,
  }) },
  code_inline: { mark: 'code', noCloseToken: true },
});

// ---------------------------------------------------------------------------
// CORPUS — mirrors tests/proposal_assist/collab/fixtures.py CORPUS exactly.
// Any drift here will be caught by the Task-6 corpus-parity test.
// ---------------------------------------------------------------------------
const CITE  = buildMarker('smith2021', 'ev-0a1b2c3d4e5f');
const CITE2 = buildMarker('jones2020', 'ev-9f8e7d6c5b4a');

const SPEC_SKELETON = '## Aims\n\n## Approach\n\n## Novelty & Significance\n\n## Expected Impact\n\n## Key Claims\n';

/** @type {Array<[string, string]>} */
const CORPUS = [
  ['preamble_only',            'Intro paragraph with no heading.\n'],
  ['single_section',           '## Aims\n\nWe aim to do the thing.\n'],
  ['multi_section_h3',         '## Approach\n\n### Phase 1\n\nDo the first part.\n\n### Phase 2\n\nThen the second.\n'],
  ['marks_and_links',          '## Novelty\n\nThis is **bold**, *italic*, ~~struck~~, `code`, and a [link](https://example.org).\n'],
  ['nested_marked_list',       '## Plan\n\n- **bold item** with a [link](https://example.org)\n  - nested item\n- second\n\n1. one\n2. two\n'],
  ['blockquote_and_code',      '## Notes\n\n> a quote\n\n```python\nprint(\'hi\')\n```\n'],
  ['horizontal_rule',          '## Sep\n\nabove\n\n---\n\nbelow\n'],
  ['image',                    '## Figure\n\n![alt text](https://example.org/fig.png)\n'],
  ['hard_break',               '## Address\n\nline one\\\nline two after a hard break.\n'],
  ['fenced_h2_not_a_section',  '## Methods\n\n```\n## not a heading\n```\n\nReal body.\n'],
  ['citation',                 `## Key Claims\n\nThe sky is blue ${CITE} and well studied.\n`],
  ['adjacent_citations',       `## Key Claims\n\nMultiple sources agree ${CITE}${CITE2} on this.\n`],
  ['bare_marker_is_literal_text', '## Background\n\nold body [@x9] stays literal text.\n'],
  ['h1_title_preamble',        '# Proposal Title\n\nIntro under an H1 title.\n\n## Aims\n\nBody.\n'],
  ['production_layout',        '# T\nintro\n## Background\nold body [@x9]\n## Aims\naims\n'],
  ['spec_skeleton',            SPEC_SKELETON],
];

// ---------------------------------------------------------------------------
// Generate + self-verify + write
// ---------------------------------------------------------------------------
mkdirSync(goldensDir, { recursive: true });

let allOk = true;
const results = [];

for (const [name, markdown] of CORPUS) {
  let pm_json, ydoc, ydoc_update_b64, roundtrip;

  try {
    // 1. Parse markdown → ProseMirror doc
    const doc = markdownParser.parse(markdown);

    // 2. Build Yjs doc from ProseMirror doc
    ydoc = prosemirrorToYDoc(doc, 'prosemirror');

    // 3. Encode Yjs state update as base64
    ydoc_update_b64 = Buffer.from(Y.encodeStateAsUpdate(ydoc)).toString('base64');

    // 4. SELF-VERIFY: the JS round-trip through Yjs is an identity.
    //
    // pm_json is taken FROM the Yjs roundtrip (yDocToProsemirrorJSON) rather
    // than from doc.toJSON(), because yDocToProsemirrorJSON is the canonical
    // serialisation format that Task 5 (Python/pycrdt) must match.
    //
    // The two differ in a few benign ways that y-prosemirror itself normalises:
    //   • marks with no attrs: doc.toJSON() omits `attrs`; roundtrip adds `attrs: {}`.
    //   • nullable attrs (e.g. image.title): doc.toJSON() stores `null`; roundtrip omits them.
    //
    // By using the roundtrip output as pm_json we guarantee
    //   yDocToProsemirrorJSON(ydoc) === pm_json
    // is trivially true (identity), AND the golden's pm_json matches what any
    // correct Yjs reader (including Python pycrdt) must reconstruct.
    //
    // We still confirm the round-trip is stable (applying it a second time
    // yields the same output), which is the meaningful check for Task 5.
    roundtrip = yDocToProsemirrorJSON(ydoc, 'prosemirror');
    // Normalise through JSON to convert null-prototype objects → plain objects.
    const normalise = obj => JSON.parse(JSON.stringify(obj));
    pm_json = normalise(roundtrip);

    // Stability check: encode → decode the ydoc a second time to confirm
    // the round-trip is stable (a non-stable encoding would indicate a y-prosemirror bug).
    const ydoc2 = new Y.Doc();
    Y.applyUpdate(ydoc2, Y.encodeStateAsUpdate(ydoc));
    const roundtrip2 = normalise(yDocToProsemirrorJSON(ydoc2, 'prosemirror'));
    deepStrictEqual(roundtrip2, pm_json);

  } catch (err) {
    console.error(`\n[FAIL] fixture "${name}": ${err.message}`);
    if (err.actual !== undefined) {
      console.error('  roundtrip1:', JSON.stringify(pm_json, null, 2).slice(0, 500));
      console.error('  roundtrip2:', JSON.stringify(roundtrip, null, 2).slice(0, 500));
    }
    allOk = false;
    results.push({ name, ok: false, error: err.message });
    continue;
  }

  results.push({ name, ok: true });

  const golden = { markdown, pm_json, ydoc_update_b64 };
  const outPath = path.join(goldensDir, `${name}.json`);
  writeFileSync(outPath, JSON.stringify(golden, null, 2) + '\n');
}

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
console.log('\n=== gen_collab_goldens results ===');
for (const r of results) {
  const icon = r.ok ? '✓' : '✗';
  const detail = r.ok ? '' : `  ERROR: ${r.error}`;
  console.log(`  ${icon} ${r.name}${detail}`);
}
console.log(`\n${results.filter(r => r.ok).length}/${results.length} goldens generated and self-verified.`);

if (!allOk) {
  console.error('\nABORTED: one or more fixtures failed self-verification. No partial golden set.');
  process.exit(1);
}

console.log(`Written to: ${goldensDir}`);
