import { useState } from "react";

import type { AdminGoldenSet as GoldenSet } from "../api/adminGoldenSet";
import { useAddTriple, useAdminGoldenSet, useDeleteTriple } from "../api/adminGoldenSet";
import styles from "./AdminGoldenSet.module.css";

type Candidate = GoldenSet["candidates"][number];

function CandidateRow({ c }: { c: Candidate }) {
  const [score, setScore] = useState("");
  const add = useAddTriple();
  return (
    <tr>
      <td>{c.call_title}</td>
      <td>{c.profile_name}</td>
      <td>{c.aggregate_score}</td>
      <td>{c.embed_similarity !== null ? c.embed_similarity.toFixed(3) : "-"}</td>
      <td><code>{c.reason}</code></td>
      <td>
        <form className={styles.inline} onSubmit={(e) => {
          e.preventDefault();
          add.mutate({ call_id: c.call_id, profile_id: c.profile_id, expected_score: Number(score) },
            { onSuccess: () => setScore("") });
        }}>
          <input type="number" min={0} max={100} placeholder="0–100" required
                 value={score} onChange={(e) => setScore(e.target.value)} />
          <button type="submit" disabled={add.isPending}>Add candidate</button>
        </form>
      </td>
    </tr>
  );
}

export function AdminGoldenSet() {
  const { data, isLoading, isError } = useAdminGoldenSet();
  const del = useDeleteTriple();
  const add = useAddTriple();
  const [callId, setCallId] = useState("");
  const [profileId, setProfileId] = useState("");
  const [score, setScore] = useState("");
  const [note, setNote] = useState("");

  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main role="alert"><p>Couldn't load the golden set.</p></main>;

  const submitManual = (e: React.FormEvent) => {
    e.preventDefault();
    add.mutate(
      { call_id: Number(callId), profile_id: Number(profileId), expected_score: Number(score), note: note.trim() || undefined },
      { onSuccess: () => { setCallId(""); setProfileId(""); setScore(""); setNote(""); } },
    );
  };

  return (
    <main className={styles.page}>
      <h1>Golden set</h1>

      <section className={styles.card}>
        <h2>Curated triples ({data.triples.length})</h2>
        {data.triples.length === 0 ? <p className={styles.muted}>No triples yet.</p> : (
          <table className={styles.table}>
            <thead><tr><th>Call</th><th>Profile</th><th>Expected score</th><th>Note</th><th></th></tr></thead>
            <tbody>
              {data.triples.map((t) => (
                <tr key={t.id}>
                  <td>{t.call_title}</td><td>{t.profile_name}</td><td>{t.expected_score}</td><td>{t.note ?? ""}</td>
                  <td><button type="button" className={styles.secondary} disabled={del.isPending}
                              onClick={() => del.mutate({ triple_id: t.id })}>Delete</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className={styles.card}>
        <h2>Add triple manually</h2>
        <form className={styles.form} onSubmit={submitManual}>
          <input type="number" placeholder="call_id" required value={callId} onChange={(e) => setCallId(e.target.value)} />
          <input type="number" placeholder="profile_id" required value={profileId} onChange={(e) => setProfileId(e.target.value)} />
          <input type="number" min={0} max={100} placeholder="Expected score (0–100)" required value={score} onChange={(e) => setScore(e.target.value)} />
          <input type="text" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button type="submit" disabled={add.isPending}>Add</button>
        </form>
      </section>

      <section className={styles.card}>
        <h2>Candidates ({data.candidates.length})</h2>
        {data.candidates.length === 0 ? (
          <p className={styles.muted}>No candidates — no dismissed-high-score, cosine-disagreement, or escalation-disagreement pairs found.</p>
        ) : (
          <table className={styles.table}>
            <thead><tr><th>Call</th><th>Profile</th><th>Score</th><th>Cosine</th><th>Signal</th><th>Add</th></tr></thead>
            <tbody>
              {data.candidates.map((c) => <CandidateRow key={`${c.call_id}-${c.profile_id}`} c={c} />)}
            </tbody>
          </table>
        )}
      </section>
    </main>
  );
}
