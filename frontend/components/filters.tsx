"use client";

import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { Select } from "./ui";

export type Range = { days: number; start: string; end: string };

export function useRange(initial = 30) {
  const [days, setDays] = useState(initial);
  // Rounded to the minute so the query key is stable between renders.
  const range = useMemo<Range>(() => {
    const end = new Date(Math.ceil(Date.now() / 60000) * 60000);
    const start = new Date(end.getTime() - days * 86400000);
    return { days, start: start.toISOString(), end: end.toISOString() };
  }, [days]);
  return { range, setDays };
}

export function RangeSelect({ days, onChange }: { days: number; onChange: (d: number) => void }) {
  return (
    <Select aria-label="Date range" value={days} onChange={(e) => onChange(Number(e.target.value))} className="h-8 w-36 text-xs">
      <option value={1}>Last 24 hours</option>
      <option value={7}>Last 7 days</option>
      <option value={30}>Last 30 days</option>
      <option value={90}>Last 90 days</option>
    </Select>
  );
}

export function WorkflowSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const key = useOrgKey();
  const { org } = useSession();
  const q = useQuery({ queryKey: [key, "workflows", "all"], queryFn: () => api<{ items: any[] }>("/workflows", { params: { limit: 200 } }), enabled: !!org });
  if (!org) return null;
  return (
    <Select aria-label="Automation" value={value} onChange={(e) => onChange(e.target.value)} className="h-8 w-48 text-xs">
      <option value="">All automations</option>
      {(q.data?.items ?? []).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
    </Select>
  );
}

export const EXEC_STATUSES = ["PENDING", "QUEUED", "RUNNING", "WAITING", "RETRYING", "COMPLETED", "FAILED", "CANCELLED"];

export function StatusSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <Select aria-label="Status" value={value} onChange={(e) => onChange(e.target.value)} className="h-8 w-36 text-xs">
      <option value="">All statuses</option>
      {EXEC_STATUSES.map((s) => <option key={s} value={s}>{s.charAt(0) + s.slice(1).toLowerCase()}</option>)}
    </Select>
  );
}
