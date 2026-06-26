import { Outlet } from "react-router-dom";

import { AdminNav } from "./AdminNav";
import { RequireAdmin } from "./RequireAdmin";

// One guard + one subnav for the whole /admin subtree. Slice-1 AdminUsers /
// AdminPendingUsers move under this layout WITHOUT their own RequireAdmin
// wrappers (collapsed here).
export function AdminLayout() {
  return (
    <RequireAdmin>
      <AdminNav />
      <Outlet />
    </RequireAdmin>
  );
}
