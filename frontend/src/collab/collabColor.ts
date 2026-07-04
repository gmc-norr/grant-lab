// Deterministic per-user presence identity. Awareness identity is display-only
// and client-spoofable — never an authorization signal (membership is enforced
// at the WS boundary).

const MAX_LABEL = 32;

export function colorForUserId(id: number): string {
  // Stable hue from the id; fixed saturation/lightness for legible carets.
  let h = 2166136261 ^ Math.trunc(id);
  h = Math.imul(h, 16777619) >>> 0;
  const hue = h % 360;
  return `hsl(${hue} 70% 45%)`;
}

export function displayName(user?: { name?: string | null; email?: string }): string {
  const raw = user?.name?.trim() || user?.email?.split("@")[0] || "Anonymous";
  return raw.length > MAX_LABEL ? raw.slice(0, MAX_LABEL) : raw;
}
