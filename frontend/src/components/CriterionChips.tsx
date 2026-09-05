import { useState } from "react";

import type { components } from "../api/schema.gen";

import styles from "./CriterionChips.module.css";

export type Criterion = components["schemas"]["CallListCriterionOut"];

// The frontend's criterion vocabulary, in READING order — and it does real work:
// the API returns criterion rows ordered by `criterion` (D5, alphabetical), which
// is not the order a person reads them in. This list is the sort key.
//
// It is also what tests/architecture/test_criterion_vocabulary.py pins, against
// BOTH places the backend writes a criterion name (ranking/worker.py's
// `gate_results` tuples and its `criterion="..."` literals) plus
// web/filters.py's label map. A criterion the backend writes and this list omits
// still renders — `label` is computed server-side and falls back to Title Case,
// so the chip is never blank — but it lands at the END of the row in arbitrary
// order, and the gate goes red so nobody has to notice that in a screenshot.
export const CRITERIA = [
  "topic_fit",
  "eligibility",
  "exclusions",
  "timeline",
  "budget",
  "trl",
  "embedding",
] as const;

type Tone = "pass" | "fail" | "unsure";

// Rating -> tone. Measured from the source, NOT from spec §4.2, which lists six
// criteria and four ratings. ranking/worker.py writes a seventh criterion
// ("embedding" -> "Topic similarity"), and ranking/criteria.py's GateStatus
// gives the four GATE criteria a third state, "unknown", that the spec does not
// mention. A chip built to the spec falls through on every gate that could not
// judge — which is most of them, because the gates return UNKNOWN whenever the
// source adapter left budget or TRL null. The other half of the same gate pins
// these keys against GateStatus ∪ ranking/prompt.py's validator tuples.
const TONE: Record<string, Tone> = {
  pass: "pass",
  fail: "fail",
  unclear: "unsure",
  unknown: "unsure",
};

const SYMBOL: Record<Tone, string> = { pass: "✓", fail: "✗", unsure: "?" };

function order(criterion: string): number {
  const i = (CRITERIA as readonly string[]).indexOf(criterion);
  return i === -1 ? CRITERIA.length : i;
}

// [tone, the text that follows the label].
function present(c: Criterion): [Tone, string] {
  if (c.is_topic_fit) {
    const n = Number(c.rating);
    // topic_fit is "1".."5". Toned at >= 4; below that the model saw a topic it
    // was not convinced by, which is the same "don't rely on this" the unsure
    // tone already means (D6). There is no third band, deliberately.
    if (c.rating.trim() !== "" && Number.isFinite(n)) {
      return [n >= 4 ? "pass" : "unsure", `${c.rating}/5`];
    }
  }
  const tone = TONE[c.rating];
  // An UNRECOGNISED rating keeps its raw string instead of collapsing into "?".
  // `call_ranking_criteria.rating` is a bare String(20) with no CHECK, so a model
  // that starts answering "probably" reaches this component unfiltered, and a
  // silent "?" would read as "the gate could not judge" — a different claim.
  return tone ? [tone, SYMBOL[tone]] : ["unsure", c.rating];
}

function hasDetail(c: Criterion): boolean {
  return Boolean(c.quoted_evidence || c.rationale);
}

// The list-view answer to "why does this match?" — one chip per criterion,
// coloured by that criterion's own rating rather than by an aggregate, and
// expanding to the funder's own words. Zero criteria (no profile selected, or an
// unranked call) renders NOTHING, not an empty row; one chip is normal, because
// the gate loop in ranking/worker.py returns on the first FAIL (D-B3).
export function CriterionChips({ criteria }: { criteria: Criterion[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (criteria.length === 0) return null;

  // Array.prototype.sort is stable, so criteria this build does not know about
  // keep the backend's relative order at the end of the row.
  const sorted = [...criteria].sort((a, b) => order(a.criterion) - order(b.criterion));
  const expanded = sorted.find((c) => c.criterion === open && hasDetail(c));

  return (
    <div className={styles.wrap}>
      <div className={styles.row}>
        {sorted.map((c) => {
          const [tone, value] = present(c);
          const body = (
            <>
              {c.label} <span className={styles.value}>{value}</span>
            </>
          );
          const attrs = {
            className: `${styles.chip} ${styles[tone]}`,
            "data-criterion": c.criterion,
            "data-tone": tone,
          };
          // A chip with neither a quote nor a rationale is not interactive: an
          // aria-expanded button that opens an empty panel is a worse lie than
          // a plain label.
          return hasDetail(c) ? (
            <button
              key={c.criterion}
              type="button"
              aria-expanded={open === c.criterion}
              onClick={() => setOpen(open === c.criterion ? null : c.criterion)}
              {...attrs}
            >
              {body}
            </button>
          ) : (
            <span key={c.criterion} {...attrs}>
              {body}
            </span>
          );
        })}
      </div>

      {expanded && (
        <div className={styles.detail}>
          {/* The funder's words FIRST, in the product's one serif, and in their
              own block — never interleaved with the model's, never nested. */}
          {expanded.quoted_evidence && (
            <blockquote className={styles.quote}>“{expanded.quoted_evidence}”</blockquote>
          )}
          {expanded.rationale && (
            <p className={styles.reasoning}>
              <span className={styles.reasoningLabel}>
                {expanded.is_reason_code ? "Reason code:" : "The model's reasoning:"}
              </span>{" "}
              {expanded.is_reason_code ? <code>{expanded.rationale}</code> : expanded.rationale}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
