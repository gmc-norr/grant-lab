export type TopTab = "draft" | "spec" | "scorecard" | "review";
export type BottomTab = "inbox" | "problems";
export type NavTarget = { top_tab?: TopTab; bottom_tab?: BottomTab };
export type OnNavigate = (target: NavTarget) => void;
