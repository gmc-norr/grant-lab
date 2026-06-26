import { useState } from "react";
import { Link } from "react-router-dom";

import type { AccountSettings } from "../api/account";
import {
  useAccountSettings,
  useChangePassword,
  useSaveSubscriptions,
  useUpdateName,
} from "../api/account";
import styles from "./Settings.module.css";

export function Settings() {
  const { data, isLoading, isError } = useAccountSettings();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load settings.</p>;
  return <SettingsForm data={data} />;
}

function SettingsForm({ data }: { data: AccountSettings }) {
  return (
    <main className={styles.settings}>
      <header className={styles.header}>
        <h1>Settings</h1>
        <p className={styles.sub}>
          Signed in as <code>{data.email}</code>
          {data.is_admin && (
            <>
              {" · "}
              <span className={styles.muted}>admin</span>
            </>
          )}
          .
        </p>
      </header>
      <NameSection initial={data.name ?? ""} />
      {data.google_oidc_enabled && <GoogleSection linked={data.google_linked} />}
      {data.has_password && <PasswordSection />}
      <SubscriptionsSection profiles={data.profiles} />
    </main>
  );
}

function NameSection({ initial }: { initial: string }) {
  const [name, setName] = useState(initial);
  const update = useUpdateName();
  return (
    <section className={styles.card}>
      <h2>Display name</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          update.mutate(name);
        }}
      >
        <label className={styles.label}>
          How should we address you?
          <input
            type="text"
            value={name}
            maxLength={200}
            placeholder="e.g. Pär Larsson"
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <button type="submit" disabled={update.isPending}>Save name</button>
        {update.isSuccess && (
          <span className={styles.ok} role="status">Saved.</span>
        )}
      </form>
    </section>
  );
}

function GoogleSection({ linked }: { linked: boolean }) {
  return (
    <section className={styles.card}>
      <h2>Google sign-in</h2>
      {linked ? (
        <p>
          Google sign-in: <strong>connected</strong>.
        </p>
      ) : (
        // Native server-side OAuth start — full-page POST, not /api. Owned by the auth slice.
        <form method="post" action="/auth/google/start">
          <input type="hidden" name="link" value="1" />
          <button type="submit">Connect Google account</button>
        </form>
      )}
    </section>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useChangePassword();
  return (
    <section className={styles.card}>
      <h2>Change password</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          change.mutate(
            { current_password: current, new_password: next },
            { onSuccess: () => { setCurrent(""); setNext(""); } },
          );
        }}
      >
        <label className={styles.label}>
          Current password
          <input
            type="password"
            autoComplete="current-password"
            required
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
          />
        </label>
        <label className={styles.label}>
          New password
          <input
            type="password"
            autoComplete="new-password"
            required
            value={next}
            onChange={(e) => setNext(e.target.value)}
          />
        </label>
        <button type="submit" disabled={change.isPending}>Change password</button>
        {change.isSuccess && (
          <span className={styles.ok} role="status">Password changed.</span>
        )}
        {change.isError && (
          <span className={styles.err} role="alert">
            {(change.error as Error | null)?.message ?? "Couldn't change password."}
          </span>
        )}
      </form>
    </section>
  );
}

function SubscriptionsSection({ profiles }: { profiles: AccountSettings["profiles"] }) {
  const [checked, setChecked] = useState<Set<number>>(
    () => new Set(profiles.filter((p) => p.subscribed).map((p) => p.id)),
  );
  const save = useSaveSubscriptions();
  if (profiles.length === 0) {
    return (
      <section className={styles.card}>
        <h2>Weekly digest subscriptions</h2>
        <p className={styles.muted}>
          No profiles to subscribe to yet — <Link to="/profiles/new">create one</Link>.
        </p>
      </section>
    );
  }
  return (
    <section className={styles.card}>
      <h2>Weekly digest subscriptions</h2>
      <p className={styles.muted}>
        Check the profiles whose Monday-morning digest you want emailed to you.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate([...checked]);
        }}
      >
        <fieldset className={styles.fieldset}>
          {profiles.map((p) => (
            <label key={p.id} className={styles.check}>
              <input
                type="checkbox"
                checked={checked.has(p.id)}
                onChange={(e) =>
                  setChecked((prev) => {
                    const nextSet = new Set(prev);
                    if (e.target.checked) nextSet.add(p.id);
                    else nextSet.delete(p.id);
                    return nextSet;
                  })
                }
              />
              {p.name}
            </label>
          ))}
        </fieldset>
        <button type="submit" disabled={save.isPending}>Save subscriptions</button>
        {save.isSuccess && (
          <span className={styles.ok} role="status">Saved.</span>
        )}
      </form>
    </section>
  );
}
