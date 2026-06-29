// frontend/src/routes/callSelection.ts

// URL helpers for the dashboard reading pane. Selection lives in the list's
// query string so it is addressable (refresh/back/share) and survives
// pagination/filter changes. Pure functions — no router, unit-testable.

// Query string (no leading "?") that selects `id` in the reading pane: copies
// the current params, sets `selected`, and pins `profile` to the list's
// effective profile when given so the pane ranks against the same profile.
export function selectionSearch(
  current: URLSearchParams,
  id: number,
  profileId?: number | null,
): string {
  const next = new URLSearchParams(current);
  next.set("selected", String(id));
  if (profileId != null) next.set("profile", String(profileId));
  return next.toString();
}

// Query string with `selected` removed (deselect / close the pane).
export function deselectSearch(current: URLSearchParams): string {
  const next = new URLSearchParams(current);
  next.delete("selected");
  return next.toString();
}
