"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, setCsrfToken, setOrganization } from "./api";

export type OrgRef = { id: string; name: string; slug: string; status: string; mode: "agency" | "saas"; role: string; permissions: string[] };
export type Me = {
  user: { id: string; email: string; full_name: string; platform_role: string | null };
  is_staff: boolean;
  platform_permissions: string[];
  organizations: OrgRef[];
  csrf_token: string;
};

type Session = {
  me: Me;
  org: OrgRef | null;
  selectOrg: (id: string | null) => void;
  can: (permission: string) => boolean;
  isStaff: boolean;
  refresh: () => Promise<unknown>;
  logout: () => Promise<void>;
};

const Ctx = createContext<Session | null>(null);
const STORAGE_KEY = "wae.org";

function stored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function useMe() {
  return useQuery<Me>({ queryKey: ["me"], queryFn: () => api("/auth/me", { org: false }), retry: false, staleTime: 60_000 });
}

export function SessionProvider({ me, children }: { me: Me; children: React.ReactNode }) {
  const qc = useQueryClient();
  const [orgId, setOrgId] = useState<string | null | undefined>(undefined);

  // Resolve the initial organization once: last used, else the first one (clients always have one selected).
  useEffect(() => {
    if (orgId !== undefined) return;
    const saved = stored();
    const match = me.organizations.find((o) => o.id === saved);
    setOrgId(match ? match.id : me.is_staff ? null : me.organizations[0]?.id ?? null);
  }, [me, orgId]);

  const org = useMemo(() => me.organizations.find((o) => o.id === orgId) ?? null, [me, orgId]);
  setCsrfToken(me.csrf_token);
  setOrganization(org?.id ?? null);

  const selectOrg = useCallback(
    (id: string | null) => {
      setOrgId(id);
      setOrganization(id);
      try {
        if (id) window.localStorage.setItem(STORAGE_KEY, id);
        else window.localStorage.removeItem(STORAGE_KEY);
      } catch {
        /* storage unavailable: selection lasts for this page only */
      }
    },
    [],
  );

  const value = useMemo<Session>(() => {
    const perms = new Set(org ? org.permissions : me.platform_permissions);
    // Staff keep their platform permissions while working inside an organization.
    if (me.is_staff) me.platform_permissions.forEach((p) => perms.add(p));
    return {
      me,
      org,
      selectOrg,
      can: (p) => perms.has(p),
      isStaff: me.is_staff,
      refresh: () => qc.invalidateQueries({ queryKey: ["me"] }),
      logout: async () => {
        try {
          await api("/auth/logout", { method: "POST", org: false });
        } finally {
          qc.clear();
          window.location.href = "/login";
        }
      },
    };
  }, [me, org, selectOrg, qc]);

  if (orgId === undefined) return null;
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSession(): Session {
  const s = useContext(Ctx);
  if (!s) throw new Error("useSession must be used inside SessionProvider");
  return s;
}

/** Query key prefix that changes with the selected organization, so data never leaks across a switch. */
export function useOrgKey(): string {
  return useSession().org?.id ?? "all";
}
