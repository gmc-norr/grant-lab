export type NavKind = "spa" | "external";

export interface ActivityItem {
  id: string;
  label: string;
  icon: string; // Codicon name without the "codicon-" prefix
  kind: NavKind; // "spa" -> react-router NavLink; "external" -> browser navigation
  to: string;
}

import type { View } from "../cockpit/nav";

/** Views of the OPEN workspace. Rendered only while one is open, below the
 *  divider. Each is a `?view` value, not a path: the rail switches which pane
 *  is showing and must not decide anything about the middle. */
export interface WorkspaceItem {
  id: View;
  label: string;
  icon: string; // Codicon name without the "codicon-" prefix
}

export const ACTIVITY_WORKSPACE: WorkspaceItem[] = [
  { id: "outline", label: "Outline", icon: "list-tree" },
  { id: "versions", label: "Versions", icon: "history" },
  { id: "references", label: "References", icon: "references" },
  { id: "criteria", label: "Criteria", icon: "checklist" },
  { id: "members", label: "Members", icon: "organization" },
];

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

// Bottom-pinned group. Profiles and Settings are both SPA routes.
export const ACTIVITY_BOTTOM: ActivityItem[] = [
  {
    id: "profiles",
    label: "Profiles",
    icon: "account",
    kind: "spa",
    to: "/profiles",
  },
  { id: "settings", label: "Settings", icon: "gear", kind: "spa", to: "/me" },
  { id: "admin", label: "Admin", icon: "shield", kind: "spa", to: "/admin" },
];
