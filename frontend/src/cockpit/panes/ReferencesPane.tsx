import { useReferences } from "../../api/citations";
import styles from "../CockpitSidebar.module.css";
import { PaneHeader } from "./PaneHeader";

export function ReferencesPane({ id }: { id: string }) {
  const references = useReferences(id);
  // Array.isArray guards keep the never-500 sidebar robust to an unexpected response
  // shape (and keep the Phase-3a tests — which stub every GET with the draft fixture —
  // green); criteria uses deep optional chaining for the same reason.
  const refs = Array.isArray(references.data) ? references.data : [];

  return (
    <>
      <PaneHeader title="References" count={refs.length || undefined} />
      {refs.length === 0 && <p className={styles.note}>No references.</p>}
      <ul className={styles.outline} aria-label="References">
        {refs.map((r) => (
          <li key={r.id} className={styles.metaRow}>
            {r.citekey && <span className={styles.metaSub}>{r.citekey}</span>}
            <span className={styles.rowLabel}>{r.title}</span>
          </li>
        ))}
      </ul>
    </>
  );
}
