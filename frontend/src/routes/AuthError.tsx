import { Link } from "react-router-dom";

import styles from "./PreAuth.module.css";

export function AuthError() {
  return (
    <>
      <h1>Sign-in could not be completed</h1>
      <p>We couldn't sign you in. Please try again.</p>
      <p>
        If you already have an account with this email, sign in your usual way, then
        connect Google from your account settings.
      </p>
      <p className={styles.link}>
        <Link to="/login">Back to sign in</Link>
      </p>
    </>
  );
}
