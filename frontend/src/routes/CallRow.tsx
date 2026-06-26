import { Link } from "react-router-dom";

import type { CallItem } from "../api/dashboard";
import { MatchBadge } from "../components/MatchBadge";
import { Deadline } from "./Deadline";
import styles from "./CallRow.module.css";

// One call in the list: title link (to the React detail route), a muted
// meta line (source · deadline), the banded match pill, and a "Start drafting"
// action that opens the React from-call create form. profileId is the dashboard's
// resolved effective profile (carried so Phase-0 can snapshot it).
export function CallRow({ call, profileId }: { call: CallItem; profileId?: number | null }) {
  const params = new URLSearchParams({ call_id: String(call.id), call_title: call.title });
  if (profileId != null) params.set("profile_id", String(profileId));
  const startHref = `/cockpit/new?${params.toString()}`;
  return (
    <li className={styles.row}>
      <div className={styles.body}>
        <Link
          className={styles.title}
          to={profileId != null ? `/calls/${call.id}?profile=${profileId}` : `/calls/${call.id}`}
        >
          {call.title}
        </Link>
        <div className={styles.meta}>
          {call.source_name && <span>{call.source_name}</span>}
          {call.source_name && call.deadline && <span aria-hidden> · </span>}
          {call.deadline && <Deadline iso={call.deadline} />}
        </div>
      </div>
      <div className={styles.actions}>
        <MatchBadge score={call.aggregate_score} />
        <Link className={styles.start} to={startHref}>
          Start drafting
        </Link>
      </div>
    </li>
  );
}
