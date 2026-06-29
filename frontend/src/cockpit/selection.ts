/** Convert a UTF-16 code-unit offset into a Unicode code-point offset, so the
 *  offsets we send match Python's code-point indexing of the same string
 *  (a non-BMP char counts as 1 code point but 2 UTF-16 units). */
export function toCodePointOffset(text: string, utf16Offset: number): number {
  return [...text.slice(0, utf16Offset)].length;
}

export const MAX_SELECTION_CODEPOINTS = 4000;

export type SelectionContext = {
  request_type: "ask" | "modify";
  target_artifact: "draft";
  section_index: number;
  base_commit_sha: string;
  base_section_sha: string;
  anchor: string;
  from_offset: number;
  to_offset: number;
};
