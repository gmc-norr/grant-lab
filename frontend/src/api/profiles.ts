import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type ProfileDetail = components["schemas"]["ProfileDetailOut"];
export type ProfileListItem = components["schemas"]["ProfileListItemOut"];
export type ProfileCreateBody = components["schemas"]["ProfileCreateIn"];
export type ProfileUpdateBody = components["schemas"]["ProfileUpdateIn"];

export function useProfilesList() {
  return useQuery({
    queryKey: ["profiles"],
    queryFn: async () => unwrap(await api.GET("/api/profiles", {})),
    retry: false,
  });
}

export function useProfile(id: number) {
  return useQuery({
    queryKey: ["profile", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/profiles/{profile_id}", { params: { path: { profile_id: id } } })),
    retry: false,
    // Poll while the bootstrap job is non-terminal; stop once it settles.
    refetchInterval: (q) => {
      const s = q.state.data?.bootstrap_status;
      return s === "queued" || s === "running" ? 3000 : false;
    },
  });
}

export function useCreateProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ProfileCreateBody) => unwrap(await api.POST("/api/profiles", { body })),
    // Without this the list served the pre-create cache for 20 s: saving your
    // FIRST profile and going back to /profiles still showed "No profiles yet"
    // and a "Create your first profile" button. `["calls"]` too — the dashboard
    // builds its Profile facet from that response (CallsFilters), so the new
    // profile is missing from the filter as well.
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["profiles"] }),
        qc.invalidateQueries({ queryKey: ["calls"] }),
      ]);
    },
  });
}

export function useUpdateProfile(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: ProfileUpdateBody) =>
      unwrap(await api.PUT("/api/profiles/{profile_id}", { params: { path: { profile_id: id } }, body })),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["profile", id] });
      qc.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}

// --- sharing: who can see this profile -----------------------------------
//
// All four endpoints are owner/admin-gated: a caller who cannot read the profile
// gets the same 404 the detail route gives, and one who can read but not edit
// gets a 403. So `retry: false` — retrying a deliberate refusal only delays the
// message.

export type ProfileViewer = components["schemas"]["ProfileViewerOut"];
export type ProfileViewers = components["schemas"]["ProfileViewersOut"];
export type ProfileVisibility = components["schemas"]["ProfileVisibilityIn"]["visibility"];

/** GET /api/profiles/{id}/viewers. `enabled` is false while the profile is
 *  org-visible: the list is meaningless then, and asking for it costs a request. */
export function useProfileViewers(id: number, enabled: boolean) {
  return useQuery({
    queryKey: ["profile-viewers", id],
    enabled,
    retry: false,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/profiles/{profile_id}/viewers", {
          params: { path: { profile_id: id } },
        }),
      ),
  });
}

/** Add one viewer BY EMAIL. There is no user picker to offer: the only endpoint
 *  that lists users is admin-only, so a non-admin owner could never learn a
 *  colleague's id. The server answers ONE generic 400 for any address it will not
 *  add — do not translate it into "no such user", which would make this a
 *  directory of who has an account. */
export function useAddProfileViewer(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (email: string) =>
      unwrap(
        await api.POST("/api/profiles/{profile_id}/viewers", {
          params: { path: { profile_id: id } },
          body: { email },
        }),
      ),
    // The response IS the server's viewer list, so seed the cache from it rather
    // than guessing what the write did (the owner's own address is a 200 no-op).
    onSuccess: (data) => {
      qc.setQueryData(["profile-viewers", id], data);
      qc.invalidateQueries({ queryKey: ["profile", id] });
    },
  });
}

/** Reconcile the viewer list to exactly these ids — the removal path. Sends
 *  NOTHING to /visibility: emptying the list leaves a private profile private. */
export function useReplaceProfileViewers(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (userIds: number[]) =>
      unwrap(
        await api.PUT("/api/profiles/{profile_id}/viewers", {
          params: { path: { profile_id: id } },
          body: { user_ids: userIds },
        }),
      ),
    onSuccess: (data) => {
      qc.setQueryData(["profile-viewers", id], data);
      qc.invalidateQueries({ queryKey: ["profile", id] });
    },
  });
}

export function useSetProfileVisibility(id: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (visibility: ProfileVisibility) =>
      unwrap(
        await api.PUT("/api/profiles/{profile_id}/visibility", {
          params: { path: { profile_id: id } },
          body: { visibility },
        }),
      ),
    // ["profiles"] too: going private can drop a profile out of a colleague's
    // list, and the list is what they navigate from.
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["profile", id] });
      qc.invalidateQueries({ queryKey: ["profiles"] });
    },
  });
}
