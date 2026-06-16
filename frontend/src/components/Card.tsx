import type { HTMLAttributes } from "react";

import styles from "./Card.module.css";

export function Card(props: HTMLAttributes<HTMLDivElement>) {
  return <div className={styles.card} {...props} />;
}
