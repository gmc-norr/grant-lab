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
