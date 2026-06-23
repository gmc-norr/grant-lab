import styles from "./MatchBadge.module.css";

// Banded score pill. The band sets the FOREGROUND color on a shared subtle chip
// surface: strong >=80 (good), good 60-79 (muted), <60 (dim). A null score (no
// ranking row for this call/profile) renders nothing.
export function MatchBadge({ score }: { score: number | null }) {
  if (score == null) return null;
  const band = score >= 80 ? "strong" : score >= 60 ? "good" : "low";
  return (
    <span className={`${styles.badge} ${styles[band]}`} data-band={band}>
      {Math.round(score)}
    </span>
  );
}
