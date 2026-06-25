export type TopTab = "draft" | "spec" | "scorecard" | "review";
export type BottomTab = "inbox" | "problems" | "activity" | "library" | "citations";
export type NavTarget = {
  top_tab?: TopTab;
  bottom_tab?: BottomTab;
  section_index?: number;
  evidence_id?: string;
};
export type OnNavigate = (target: NavTarget) => void;
