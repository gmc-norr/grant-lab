import { Link } from "react-router-dom";

import styles from "./PreAuth.module.css";

export function AccountPending() {
  return (
    <>
      <h1>Account awaiting approval</h1>
      <p>
        Your sign-in worked. An administrator needs to approve your account before
        you can use Call Manager. You'll be able to sign in once approved.
      </p>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
