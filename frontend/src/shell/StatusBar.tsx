import styles from "./StatusBar.module.css";

// Phase 1: a minimal global status bar. Cockpit-specific content (branch,
// deadline, spec-lock, scorecard band, Ln/Col, language mode) is wired in
// Phase 3 when the cockpit reconciles into the shell regions.
export function StatusBar() {
  return (
    <footer className={styles.statusbar} aria-label="Status bar">
      <span className={styles.spacer} />
      <span className={styles.item}>Call Manager</span>
    </footer>
  );
}
