import type { RubricCriterion } from "../api/rubric";

export function RubricEditor({
  criteria,
  funderNotes,
  onChange,
}: {
  criteria: RubricCriterion[];
  funderNotes: string;
  onChange: (next: { criteria: RubricCriterion[]; funderNotes: string }) => void;
}) {
  const total = criteria.reduce((s, c) => s + (Number(c.weight) || 0), 0);
  const patch = (i: number, field: keyof RubricCriterion, value: unknown) =>
    onChange({
      // spread preserves passthrough fields (subcriteria, etc.)
      criteria: criteria.map((c, idx) => (idx === i ? { ...c, [field]: value } : c)),
      funderNotes,
    });

  return (
    <div className="rubric-editor">
      <table>
        <thead>
          <tr><th>Criterion</th><th>Description</th><th>Weight</th><th>%</th><th>Scale max</th></tr>
        </thead>
        <tbody>
          {criteria.map((c, i) => (
            <tr key={i}>
              <td>
                <input aria-label={`Name for ${c.name}`} value={c.name}
                  onChange={(e) => patch(i, "name", e.target.value)} />
              </td>
              <td>
                <textarea aria-label={`Description for ${c.name}`} value={c.description}
                  onChange={(e) => patch(i, "description", e.target.value)} />
              </td>
              <td>
                <input type="number" min={0} step={0.01} aria-label={`Weight for ${c.name}`}
                  value={c.weight} onChange={(e) => patch(i, "weight", Number(e.target.value))} />
              </td>
              <td>{total > 0 ? Math.round((Number(c.weight) / total) * 100) : 0}%</td>
              <td>
                <input type="number" min={1} max={10} aria-label={`Scale max for ${c.name}`}
                  value={c.scale_max} onChange={(e) => patch(i, "scale_max", Number(e.target.value))} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <label className="rubric-notes">
        Funder notes
        <textarea value={funderNotes} onChange={(e) => onChange({ criteria, funderNotes: e.target.value })} />
      </label>
    </div>
  );
}
