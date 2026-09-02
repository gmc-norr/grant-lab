import type { components } from "../api/schema.gen";

/** Convert a UTF-16 code-unit offset into a Unicode code-point offset, so the
 *  offsets we send match Python's code-point indexing of the same string
 *  (a non-BMP char counts as 1 code point but 2 UTF-16 units). */
export function toCodePointOffset(text: string, utf16Offset: number): number {
  // oxlint's `unicorn/no-useless-spread` fires here and is WRONG: it reports
  // that `text.slice` "returns a new array", but String.prototype.slice returns
  // a STRING. The spread is what iterates CODE POINTS — dropping it turns this
  // into `text.slice(...).length`, i.e. UTF-16 code units, which is precisely
  // the off-by-N this function exists to prevent for non-BMP characters.
  // oxlint-disable-next-line no-useless-spread
  return [...text.slice(0, utf16Offset)].length;
}

export const MAX_SELECTION_CODEPOINTS = 4000;

/** The selection payload posted with a turn. Derived from the generated
 *  OpenAPI schema so it stays in lockstep with the backend Pydantic model —
 *  a field rename/retype on the server fails the build here, not silently at
 *  the call site. */
export type SelectionContext = components["schemas"]["SelectionContextIn"];
