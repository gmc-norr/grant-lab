export type TopTab = "draft" | "spec" | "scorecard" | "review" | "diff";
export type View = "outline" | "versions" | "references" | "criteria" | "members";
export type BottomTab = "inbox" | "problems" | "activity" | "library" | "citations" | "claims";
export type NavTarget = {
  top_tab?: TopTab;
  bottom_tab?: BottomTab;
  section_index?: number;
  evidence_id?: string;
};
export type OnNavigate = (target: NavTarget) => void;
