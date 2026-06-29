// The "research crystal" brand mark: a hexagonal knowledge-network with a teal
// center node. Simplified radial form (clean at small sizes). Strokes/nodes use
// theme CSS vars so it adapts; the center node is the teal accent.
export function BrandMark({ size = 20 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 256 256"
      role="img"
      aria-label="Grant Lab"
    >
      <path
        d="M128 22 218 74v108l-90 52-90-52V74z"
        fill="none"
        stroke="var(--app-text)"
        strokeWidth="14"
        strokeLinejoin="round"
      />
      <g stroke="var(--app-dim)" strokeWidth="10" strokeLinecap="round">
        <path d="M128 128 128 56M128 128 82 84M128 128 174 84M128 128 82 172M128 128 174 172M128 128 128 200" />
      </g>
      <g fill="var(--app-text)">
        <circle cx="128" cy="56" r="13" />
        <circle cx="82" cy="84" r="13" />
        <circle cx="174" cy="84" r="13" />
        <circle cx="82" cy="172" r="13" />
        <circle cx="174" cy="172" r="13" />
        <circle cx="128" cy="200" r="13" />
      </g>
      <circle
        cx="128"
        cy="128"
        r="22"
        fill="var(--app-accent-bright)"
        stroke="var(--app-editor)"
        strokeWidth="7"
      />
    </svg>
  );
}
