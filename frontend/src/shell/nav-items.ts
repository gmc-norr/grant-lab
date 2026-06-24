export type NavKind = "spa" | "external";

export interface ActivityItem {
  id: string;
  label: string;
  icon: string; // Codicon name without the "codicon-" prefix
  kind: NavKind; // "spa" -> react-router NavLink; "external" -> Jinja page <a>
  to: string;
}

// Primary group. The active Workspace/cockpit item is NOT here — ActivityBar
// renders it dynamically only while a cockpit is open. References is a
// per-workspace concern (cockpit sidebar), never a top-level destination.
export const ACTIVITY_TOP: ActivityItem[] = [
  { id: "calls", label: "Calls", icon: "search", kind: "spa", to: "/" },
  {
    id: "workspaces",
    label: "Workspaces",
    icon: "folder",
    kind: "spa",
    to: "/cockpit",
  },
];

// Bottom-pinned group. These mirror the existing Jinja nav exactly
// (base.html: /profiles, /me) and deep-link out until those pages migrate.
export const ACTIVITY_BOTTOM: ActivityItem[] = [
  {
    id: "profiles",
    label: "Profiles",
    icon: "account",
    kind: "external",
    to: "/profiles",
  },
  { id: "settings", label: "Settings", icon: "gear", kind: "external", to: "/me" },
];
