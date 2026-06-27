import { Link } from "react-router-dom";

import styles from "./PreAuth.module.css";

export function AccountNoAccess() {
  return (
    <>
      <h1>No access</h1>
      <p>
        This account does not have access. If you think this is a mistake, contact
        an administrator.
      </p>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
