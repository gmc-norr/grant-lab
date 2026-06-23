import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";
import styles from "./CallRow.module.css";

// One call in the list: title link (to the un-ported Jinja detail page), a muted
// meta line (source · deadline), and the banded match pill. Plain <a>, not a
// router link — coexistence with the un-ported /calls/{id} HTML page.
export function CallRow({ call }: { call: CallItem }) {
  return (
    <li className={styles.row}>
      <div className={styles.body}>
        <a className={styles.title} href={`/calls/${call.id}`}>
          {call.title}
        </a>
        <div className={styles.meta}>
          {call.source_name && <span>{call.source_name}</span>}
          {call.source_name && call.deadline && <span aria-hidden> · </span>}
          {call.deadline && <Deadline iso={call.deadline} />}
        </div>
      </div>
      <MatchBadge score={call.aggregate_score} />
    </li>
  );
}
