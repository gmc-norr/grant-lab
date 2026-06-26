import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap } from "./client";
import type { components } from "./schema.gen";

export type AdminGoldenSet = components["schemas"]["AdminGoldenSetOut"];

export function useAdminGoldenSet() {
  return useQuery({
    queryKey: ["admin-golden-set"],
    queryFn: async () => unwrap(await api.GET("/api/admin/golden-set", {})),
    retry: false,
  });
}

export function useAddTriple() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { call_id: number; profile_id: number; expected_score: number; note?: string }) =>
      unwrap(await api.POST("/api/admin/golden-set", { body: v })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-golden-set"] }),
  });
}

export function useDeleteTriple() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { triple_id: number }) =>
      unwrap(await api.DELETE("/api/admin/golden-set/{triple_id}", { params: { path: { triple_id: v.triple_id } } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin-golden-set"] }),
  });
}
