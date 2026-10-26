// frontend/src/components/RankingFailedNote.tsx
import styles from "./RankingFailedNote.module.css";

// "THE SCORE YOU ARE LOOKING AT IS NOT CURRENT." Review 2026-10-26 (A4 finding
// 2): since the 2026-10-06 Ollama upgrade the nightly re-rank of 19 long EU
// calls failed about twice a night, and 13 of those calls went on showing a
// score computed on the PREVIOUS model as though it had just been produced.
// Nothing on any surface said so — `_ranking_counts` deliberately counts only
// pairs with no ranking at all, so a pair with a stale one was invisible by
// construction.
//
// ONE component for every surface that prints the sentence, so the copy cannot
// drift between the card and the detail page. Plain markup and a CSS module
// rather than Mantine `Text`: the triage card (TriageCard.tsx) is deliberately
// Mantine-free, and this has to render inside it.
//
// `failed === false` renders NOTHING, not an empty element: the healthy case is
// the overwhelming majority of rows and must cost no vertical space.
export function RankingFailedNote({
  failed,
  at,
  className,
}: {
  failed: boolean;
  at: string | null;
  className?: string;
}) {
  if (!failed) return null;
  // The instant's UTC calendar date, the same slice Deadline.tsx takes — the
  // reader needs "which night", not a time. `at` is non-null whenever `failed`
  // is true (the API's invariant, asserted in tests/test_api_calls.py), so the
  // bare-sentence branch is a guard against a future shape, not a live state.
  const date = at ? at.slice(0, 10) : null;
  return (
    <p data-field="ranking-failed" className={[styles.note, className].filter(Boolean).join(" ")}>
      Couldn&apos;t rank this call with the local model
      {date !== null && ` (last tried ${date})`}
    </p>
  );
}
