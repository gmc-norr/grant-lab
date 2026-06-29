import styles from "./CockpitChat.module.css";

type Ref = { id: string; citekey?: string | null; title: string };
type Turn = { id: string; role: string; content: unknown; position: number };
type Block = Record<string, unknown>;

function blocks(content: unknown): Block[] {
  if (Array.isArray(content)) return content as Block[];
  if (content && typeof content === "object") return [content as Block];
  if (typeof content === "string") return [{ type: "text", text: content }];
  return [];
}

function RefChip({ id, refs }: { id: string; refs: Ref[] }) {
  const r = refs.find((x) => x.id === id);
  return <span className={styles.refChip}>{r ? (r.citekey ?? r.title) : "reference"}</span>;
}

function TurnItem({ id, turn, refs }: { id: string; turn: Turn; refs: Ref[] }) {
  const bs = blocks(turn.content);
  // Tool rows render as one compact, low-emphasis line.
  if (turn.role === "tool") {
    const name = bs.map((b) => (b.tool_name as string) || (b.type as string)).join(" · ");
    return <div className={styles.tool}>⚙ {name}</div>;
  }
  const cls = turn.role === "user" ? styles.userTurn : styles.assistantTurn;
  return (
    <div className={cls}>
      {bs.map((b, i) => {
        if (b.type === "turn_outcome") return <div key={`${turn.id}-${i}`} className={styles.outcome}>✓ {String(b.text ?? "")}</div>;
        if (b.type === "reference") return <RefChip key={`${turn.id}-${i}`} id={String(b.reference_id)} refs={refs} />;
        if (b.type === "selection") {
          const verb = (b.request_type as string) === "modify" ? "Edit" : "Ask";
          const verified = b.verified as boolean;
          return (
            <blockquote key={`${turn.id}-${i}`} className={styles.selectionChip} data-verb={String(b.request_type)}>
              <span className={styles.selectionVerb}>{verb} ·{verified ? "" : " unsaved"}</span>
              {String(b.anchor ?? "")}
            </blockquote>
          );
        }
        if (b.type === "image" && typeof b.attachment_id === "string") {
          return <img key={`${turn.id}-${i}`} className={styles.chatImage} src={`/api/workspaces/${id}/files/${b.attachment_id}`} alt="attached image" />;
        }
        if (b.type === "text" || typeof b.text === "string") return <span key={`${turn.id}-${i}`}>{String(b.text ?? "")}</span>;
        return <span key={`${turn.id}-${i}`} className={styles.note}>·</span>; // unknown block: muted, never throws
      })}
    </div>
  );
}

export function TurnList({ id, turns, refs }: { id: string; turns: Turn[]; refs: Ref[] }) {
  return (
    <div className={styles.transcript}>
      {turns.map((t) => (
        <TurnItem key={t.id} id={id} turn={t} refs={refs} />
      ))}
    </div>
  );
}
