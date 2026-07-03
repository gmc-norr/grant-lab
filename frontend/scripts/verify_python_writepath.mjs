/**
 * Cross-runtime WRITE-PATH verifier (Python -> Node direction).
 *
 * This is the deferred Slice-0 cross-runtime check in the direction Slice-0
 * left open: a ydoc built by the pure-Python `pm_json_to_ydoc` must be read
 * IDENTICALLY by a real browser `y-prosemirror`. It reads the committed
 * artifact `tests/proposal_assist/collab/goldens/python_writepath.json`
 * (produced by the Python `test_collab_writepath.py`), and for each fixture:
 *
 *   1. applies the Python-produced Yjs update bytes into a fresh `Y.Doc`, then
 *   2. reads it back with `yDocToProsemirrorJSON(doc, "prosemirror")`, and
 *   3. deep-equals the reconstruction to the entry's `pm_json`.
 *
 * Normalisation matches Slice-0 `gen_collab_goldens.mjs` exactly:
 * `JSON.parse(JSON.stringify(x))` (null-prototype objects -> plain objects; this
 * is also what drops absent `attrs`/null attrs consistently on both sides,
 * since both sides go through the same normalise).
 *
 * ANY mismatch is a real NO-GO for the Python write path — exit non-zero.
 * The per-fixture comparison is exported so the vitest wrapper can reuse it.
 */

import { deepStrictEqual } from 'assert';
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

import { yDocToProsemirrorJSON } from 'y-prosemirror';
import * as Y from 'yjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const frontendDir = path.resolve(__dirname, '..');
const repoRoot = path.resolve(frontendDir, '..');
const ARTIFACT = path.resolve(
  repoRoot,
  'tests/proposal_assist/collab/goldens/python_writepath.json',
);

const INCREMENTAL = path.resolve(
  repoRoot,
  'tests/proposal_assist/collab/goldens/python_writepath_incremental.json',
);

const normalise = (o) => JSON.parse(JSON.stringify(o));

/** Load the committed Python-produced write-path artifact. */
export function loadWritepath() {
  return JSON.parse(readFileSync(ARTIFACT, 'utf8'));
}

/**
 * Reconstruct one entry's PM JSON from the Python-produced Yjs bytes.
 * @returns {object} normalised ProseMirror JSON read back via y-prosemirror.
 */
export function reconstruct(entry) {
  const d = new Y.Doc();
  Y.applyUpdate(d, Buffer.from(entry.update_b64, 'base64'));
  return normalise(yDocToProsemirrorJSON(d, 'prosemirror'));
}

/** Load the committed Python-produced incremental write-path artifact. */
export function loadIncremental() {
  return JSON.parse(readFileSync(INCREMENTAL, 'utf8'));
}

/** Apply base THEN incremental Python-produced bytes; read back the PM JSON. */
export function reconstructIncremental(entry) {
  const d = new Y.Doc();
  Y.applyUpdate(d, Buffer.from(entry.base_b64, 'base64'));
  Y.applyUpdate(d, Buffer.from(entry.incr_b64, 'base64'));
  return normalise(yDocToProsemirrorJSON(d, 'prosemirror'));
}

export function compareAllIncremental(entries = loadIncremental()) {
  const mismatches = [];
  for (const entry of entries) {
    try {
      deepStrictEqual(reconstructIncremental(entry), normalise(entry.pm_json));
    } catch {
      mismatches.push({ name: entry.name });
    }
  }
  return mismatches;
}

/**
 * Compare all entries. Returns the list of mismatches (empty == all identical).
 * Each mismatch carries the fixture name and both sides for debugging.
 */
export function compareAll(entries = loadWritepath()) {
  const mismatches = [];
  for (const entry of entries) {
    const expected = normalise(entry.pm_json);
    const actual = reconstruct(entry);
    // Deep, ORDER-INSENSITIVE equality — the same semantics Slice-0
    // `gen_collab_goldens.mjs` used (`deepStrictEqual`). Object-key order is not
    // semantically significant: y-prosemirror may emit a mark's attrs
    // (e.g. link `{href, title}`) in a different key order than the
    // Python producer, which a byte-wise `JSON.stringify` compare would wrongly
    // flag. `deepStrictEqual` treats those as equal, as they are.
    try {
      deepStrictEqual(actual, expected);
    } catch {
      mismatches.push({ name: entry.name, expected, actual });
    }
  }
  return mismatches;
}

// ---------------------------------------------------------------------------
// CLI entry: run only when invoked directly (not when imported by vitest).
// ---------------------------------------------------------------------------
const invokedDirectly =
  process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (invokedDirectly) {
  const entries = loadWritepath();
  const mismatches = compareAll(entries);
  const bad = new Set(mismatches.map((m) => m.name));

  console.log('\n=== verify_python_writepath (Node reads Python-produced ydocs) ===');
  for (const entry of entries) {
    console.log(`  ${bad.has(entry.name) ? '✗' : '✓'} ${entry.name}`);
  }

  if (mismatches.length) {
    console.error(`\nNO-GO: ${mismatches.length}/${entries.length} fixture(s) mismatched:`);
    for (const m of mismatches) {
      console.error(`\n[MISMATCH] ${m.name}`);
      console.error('  expected:', JSON.stringify(m.expected).slice(0, 800));
      console.error('  actual:  ', JSON.stringify(m.actual).slice(0, 800));
    }
    process.exit(1);
  }

  console.log(
    `\nGO: ${entries.length}/${entries.length} fixtures read back identical ` +
      `(Node y-prosemirror == Python pm_json_to_ydoc).`,
  );

  // --- Incremental fixtures ---
  const incrEntries = loadIncremental();
  const incrMismatches = compareAllIncremental(incrEntries);
  const incrBad = new Set(incrMismatches.map((m) => m.name));

  console.log('\n=== verify_python_writepath incremental (base + apply_pm_section_to_fragment) ===');
  for (const entry of incrEntries) {
    console.log(`  ${incrBad.has(entry.name) ? '✗' : '✓'} ${entry.name}`);
  }

  if (incrMismatches.length) {
    console.error(
      `\nNO-GO: ${incrMismatches.length}/${incrEntries.length} incremental fixture(s) mismatched:`,
    );
    for (const m of incrMismatches) {
      console.error(`\n[MISMATCH] ${m.name}`);
    }
    process.exit(1);
  }

  console.log(
    `\nGO: ${incrEntries.length}/${incrEntries.length} incremental fixtures read back identical ` +
      `(Node y-prosemirror == Python apply_pm_section_to_fragment).`,
  );
  process.exit(0);
}
