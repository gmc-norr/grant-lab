export type TopTab = "draft" | "spec" | "scorecard" | "review";
export type BottomTab = "inbox" | "problems" | "activity" | "library";
export type NavTarget = { top_tab?: TopTab; bottom_tab?: BottomTab; section_index?: number };
export type OnNavigate = (target: NavTarget) => void;
