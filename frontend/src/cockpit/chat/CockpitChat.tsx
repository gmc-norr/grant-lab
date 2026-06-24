import { useParams } from "react-router-dom";

import { useTurns } from "../../api/turns";
import styles from "./CockpitChat.module.css";

export function CockpitChat() {
  const { id = "" } = useParams();
  const turns = useTurns(id);
  return (
    <aside className={styles.chat} aria-label="Chat">
      <header className={styles.header}>Chat</header>
      <div className={styles.body}>
        {turns.isLoading && <p className={styles.note}>Loading…</p>}
        {/* TurnList + footer-gate land in Tasks 7-8 */}
      </div>
    </aside>
  );
}
