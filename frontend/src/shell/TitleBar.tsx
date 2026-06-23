import { useLocation } from "react-router-dom";

import { BrandMark } from "./BrandMark";
import styles from "./TitleBar.module.css";

function titleForPath(pathname: string): string {
  if (pathname.startsWith("/cockpit")) return "Proposal workspace";
  return "Funding calls";
}

export function TitleBar() {
  const { pathname } = useLocation();
  return (
    <header className={styles.titlebar}>
      <span className={styles.brand}>
        <BrandMark size={18} />
        <span className={styles.app}>Call Manager</span>
      </span>
      <span className={styles.title}>{titleForPath(pathname)}</span>
    </header>
  );
}
