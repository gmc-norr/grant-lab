import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useProfile, useUpdateProfile, type ProfileDetail, type ProfileUpdateBody } from "../api/profiles";
import styles from "./ProfileForm.module.css";
import { StructuredFields, type StructuredValues } from "./StructuredFields";

const LEVELS = [
  ["", "(unspecified)"], ["lab", "Lab"], ["group", "Research group"], ["department", "Department"],
  ["clinic", "Clinic"], ["institution", "Institution"], ["other", "Other"],
];
const CURRENCIES = ["", "SEK", "EUR", "USD", "NOK", "DKK"];

function joinLines(xs: string[]) { return xs.join("\n"); }

export function ProfileEdit() {
  const { id } = useParams();
  const pid = Number(id);
  const { data, isLoading, isError } = useProfile(pid);
  if (isLoading) return <p>Loading…</p>;
  if (isError || !data) return <main className={styles.page}><p role="alert">Profile not found.</p></main>;
  return <EditForm key={data.bootstrap_status ?? "ready"} p={data} pid={pid} />;
}

function EditForm({ p, pid }: { p: ProfileDetail; pid: number }) {
  const navigate = useNavigate();
  const update = useUpdateProfile(pid);
  const drafting = p.bootstrap_status === "queued" || p.bootstrap_status === "running";
  const locked = drafting || !p.can_edit;

  const [description, setDescription] = useState(p.description);
  const [level, setLevel] = useState(p.level ?? "");
  const [budgetMin, setBudgetMin] = useState(p.budget_min?.toString() ?? "");
  const [budgetMax, setBudgetMax] = useState(p.budget_max?.toString() ?? "");
  const [currency, setCurrency] = useState(p.budget_currency ?? "");
  const [trlMin, setTrlMin] = useState(p.trl_min?.toString() ?? "");
  const [trlMax, setTrlMax] = useState(p.trl_max?.toString() ?? "");
  const [mustInclude, setMustInclude] = useState(joinLines(p.must_include));
  const [hardExclusions, setHardExclusions] = useState(joinLines(p.hard_exclusions));
  const [struct, setStruct] = useState<StructuredValues>({
    areas: joinLines(p.areas), career_stage: p.career_stage ?? "", applicant_types: joinLines(p.applicant_types),
    role: p.role ?? "", years_since_phd: p.years_since_phd?.toString() ?? "", org_type: p.org_type ?? "",
    capabilities: joinLines(p.capabilities), patient_populations: joinLines(p.patient_populations),
    scale: p.scale ?? "", funding_instruments: joinLines(p.funding_instruments),
    funder_exclusions: joinLines(p.funder_exclusions), country: p.country ?? "", host_institution: p.host_institution ?? "",
  });
  const setS = (k: keyof StructuredValues, v: string) => setStruct((s) => ({ ...s, [k]: v }));

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const body: ProfileUpdateBody = {
      description, level, budget_min: budgetMin, budget_max: budgetMax, budget_currency: currency,
      trl_min: trlMin, trl_max: trlMax, must_include: mustInclude, hard_exclusions: hardExclusions, ...struct,
    };
    update.mutate(body, { onSuccess: () => navigate(`/profiles/${pid}`) });
  }

  return (
    <main className={styles.page}>
      <Link className={styles.back} to={`/profiles/${pid}`}>← Back to profile</Link>
      <h1>Edit {p.name}</h1>
      <p className={styles.lede}>
        Description feeds the semantic similarity gate. Areas, budgets, TRL, must-include and exclusions feed the
        deterministic rubric gates. Saving kicks off a backfill of the last 90 days against this profile.
      </p>
      {drafting && (
        <div className={styles.banner}>⏳ Drafting your profile from PubMed and past grants — this can take a minute on the local model.</div>
      )}
      {p.bootstrap_status === "done" && (
        <div className={styles.banner}>
          Draft ready.
          {p.bootstrap_pubmed_status === "ok" && ` Found ${p.publications_fetched} publications for your members.`}
          {p.bootstrap_pubmed_status === "empty" && " No publications matched your members on PubMed — the draft rests on your mission text and pasted grants."}
          {p.bootstrap_pubmed_status === "unavailable" && " PubMed was unavailable, so the draft rests on your mission text and pasted grants."}
        </div>
      )}
      {p.bootstrap_status === "failed" && (
        <div className={`${styles.banner} ${styles.bannerWarn}`}>We couldn't auto-draft a description (the local model was unavailable). Write one below and save.</div>
      )}
      <form className={styles.form} onSubmit={submit}>
        <fieldset disabled={locked}>
          <label>Description
            <textarea rows={6} value={description} onChange={(e) => setDescription(e.target.value)} />
            <small>Changing this triggers a background re-embed.</small>
          </label>
          {p.kind === "organizational_unit" && (
            <label>Level
              <select value={level} onChange={(e) => setLevel(e.target.value)}>
                {LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          )}
          <StructuredFields kind={p.kind} values={struct} onChange={setS} disabled={locked} />
          <label>Budget min
            <input type="text" value={budgetMin} onChange={(e) => setBudgetMin(e.target.value)} />
          </label>
          <label>Budget max
            <input type="text" value={budgetMax} onChange={(e) => setBudgetMax(e.target.value)} />
          </label>
          <label>Currency
            <select value={currency} onChange={(e) => setCurrency(e.target.value)}>
              {CURRENCIES.map((c) => <option key={c} value={c}>{c || "(unknown)"}</option>)}
            </select>
          </label>
          <label>Technology Readiness Level — min <small>(1–9)</small>
            <input type="text" value={trlMin} onChange={(e) => setTrlMin(e.target.value)} />
          </label>
          <label>Technology Readiness Level — max <small>(1–9)</small>
            <input type="text" value={trlMax} onChange={(e) => setTrlMax(e.target.value)} />
          </label>
          <label>Must-include keywords <small>(one per line; bypass the timeline gate)</small>
            <textarea rows={3} value={mustInclude} onChange={(e) => setMustInclude(e.target.value)} />
          </label>
          <label>Hard exclusions <small>(one per line)</small>
            <textarea rows={3} value={hardExclusions} onChange={(e) => setHardExclusions(e.target.value)} />
          </label>
          <div className={styles.actions}>
            <button type="submit" disabled={locked || update.isPending}>Save and re-rank</button>
            <Link to={`/profiles/${pid}`}>Cancel</Link>
            {update.isError && (
              <span className={styles.err} role="alert">
                {(update.error as Error | null)?.message ?? "Couldn't save."}
              </span>
            )}
          </div>
        </fieldset>
      </form>
    </main>
  );
}
