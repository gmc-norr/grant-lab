import { Link } from "react-router-dom";

import { useProfilesList } from "../api/profiles";
import styles from "./ProfilesList.module.css";

export function ProfilesList() {
  const { data, isLoading, isError } = useProfilesList();
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <p role="alert">Couldn't load profiles.</p>;
  const profiles = data.profiles;
  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <hgroup>
          <h1>Ranking profiles</h1>
          <p className={styles.sub}>
            Each profile defines who you are and what you fund-hunt for. The LLM ranks calls against the
            description and rubric.
          </p>
        </hgroup>
        {profiles.length > 0 && (
          <Link className={styles.newBtn} to="/profiles/new">+ New profile</Link>
        )}
      </header>
      {profiles.length === 0 ? (
        <div className={styles.empty}>
          <h3>No profiles yet</h3>
          <p>Create one to see ranked calls on the dashboard.</p>
          <Link to="/profiles/new">Create your first profile</Link>
        </div>
      ) : (
        <div className={styles.cards}>
          {profiles.map((p) => (
            <article key={p.id} className={styles.card}>
              <h3><Link to={`/profiles/${p.id}`}>{p.name}</Link></h3>
              <div className={styles.meta}>
                {p.kind}
                {p.areas_count > 0 && ` · ${p.areas_count} areas`}
                {p.budget_currency && ` · ${p.budget_currency}`}
              </div>
              {p.description && (
                <p className={styles.desc}>
                  {p.description.slice(0, 160)}{p.description.length > 160 ? "…" : ""}
                </p>
              )}
              <div className={styles.links}>
                <Link to={`/?profile=${p.id}`}>Ranked feed →</Link>
                {" · "}
                <Link to={`/profiles/${p.id}/edit`}>Edit</Link>
              </div>
            </article>
          ))}
        </div>
      )}
    </main>
  );
}
