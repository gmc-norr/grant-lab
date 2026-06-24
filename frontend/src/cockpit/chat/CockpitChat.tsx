import { useParams } from "react-router-dom";

import { useReferences } from "../../api/citations";
import { useTurns } from "../../api/turns";
import styles from "./CockpitChat.module.css";
import { TurnList } from "./TurnList";

export function CockpitChat() {
  const { id = "" } = useParams();
  const turns = useTurns(id);
  const references = useReferences(id);
  const refs = Array.isArray(references.data) ? references.data : [];
  return (
    <aside className={styles.chat} aria-label="Chat">
      <header className={styles.header}>Chat</header>
      <div className={styles.body}>
        {turns.isLoading && <p className={styles.note}>Loading…</p>}
        {turns.data && <TurnList turns={turns.data.turns} refs={refs} />}
      </div>
    </aside>
  );
}
