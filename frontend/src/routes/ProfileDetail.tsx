import { Link, useParams } from "react-router-dom";

import type { ProfileDetail as Detail } from "../api/profiles";
import { useProfile } from "../api/profiles";
import styles from "./ProfileDetail.module.css";

function Chips({ items }: { items: string[] }) {
  if (items.length === 0) return <>—</>;
  return <>{items.map((c, i) => <span key={i} className={styles.chip}>{c}</span>)}</>;
}

function range(min: number | null, max: number | null) {
  if (min == null && max == null) return "—";
  return `${min ?? "—"} – ${max ?? "—"}`;
}

export function ProfileDetail() {
  const { id } = useParams();
  const { data, isLoading, isError } = useProfile(Number(id));
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main className={styles.page}><p role="alert">Profile not found.</p></main>;
  return <DetailView p={data} />;
}

function DetailView({ p }: { p: Detail }) {
  const hasProfileFields =
    p.org_type || p.capabilities.length || p.patient_populations.length || p.scale ||
    p.career_stage || p.applicant_types.length || p.role || p.years_since_phd != null ||
    p.funding_instruments.length || p.funder_exclusions.length || p.country || p.host_institution;
  return (
    <main className={styles.page}>
      <Link className={styles.back} to="/profiles">← All profiles</Link>
      <header className={styles.header}>
        <hgroup>
          <h1>{p.name}</h1>
          <p className={styles.kind}>{p.kind}{p.level ? ` · ${p.level}` : ""}</p>
        </hgroup>
        <div className={styles.actions}>
          <Link className={styles.primary} to={`/?profile=${p.id}`}>Ranked feed →</Link>
          <Link to={`/profiles/${p.id}/edit`}>Edit</Link>
        </div>
      </header>

      <section className={styles.card}>
        <h2>Description</h2>
        <p>{p.description || "— (no description yet)"}</p>
      </section>

      <section className={styles.card}>
        <h2>Ranking rubric</h2>
        <dl>
          <dt>Areas</dt><dd><Chips items={p.areas} /></dd>
          <dt>Budget</dt>
          <dd>{p.budget_min == null && p.budget_max == null ? "—"
            : `${p.budget_min?.toLocaleString() ?? "—"} – ${p.budget_max?.toLocaleString() ?? "—"}${p.budget_currency ? ` ${p.budget_currency}` : ""}`}</dd>
          <dt>Technology Readiness Level</dt><dd>{range(p.trl_min, p.trl_max)}</dd>
          <dt>Must include</dt><dd><Chips items={p.must_include} /></dd>
          <dt>Hard exclusions</dt><dd><Chips items={p.hard_exclusions} /></dd>
        </dl>
      </section>

      {hasProfileFields && (
        <section className={styles.card}>
          <h2>Profile</h2>
          <dl>
            {p.org_type && (<><dt>Organization type</dt><dd>{p.org_type}</dd></>)}
            {p.capabilities.length > 0 && (<><dt>Capabilities</dt><dd><Chips items={p.capabilities} /></dd></>)}
            {p.patient_populations.length > 0 && (<><dt>Patient populations</dt><dd><Chips items={p.patient_populations} /></dd></>)}
            {p.scale && (<><dt>Scale</dt><dd>{p.scale}</dd></>)}
            {p.career_stage && (<><dt>Career stage</dt><dd>{p.career_stage}</dd></>)}
            {p.role && (<><dt>Role</dt><dd>{p.role}</dd></>)}
            {p.years_since_phd != null && (<><dt>Years since PhD</dt><dd>{p.years_since_phd}</dd></>)}
            {p.applicant_types.length > 0 && (<><dt>Applicant types</dt><dd><Chips items={p.applicant_types} /></dd></>)}
            {p.funding_instruments.length > 0 && (<><dt>Funding instruments</dt><dd><Chips items={p.funding_instruments} /></dd></>)}
            {p.funder_exclusions.length > 0 && (<><dt>Funder exclusions</dt><dd><Chips items={p.funder_exclusions} /></dd></>)}
            {p.host_institution && (<><dt>Host institution</dt><dd>{p.host_institution}</dd></>)}
            {p.country && (<><dt>Country</dt><dd>{p.country}</dd></>)}
          </dl>
        </section>
      )}
    </main>
  );
}
