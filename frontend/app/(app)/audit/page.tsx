"use client";

import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ScrollText } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDate } from "@/lib/format";
import { RangeSelect, useRange } from "@/components/filters";
import { Card, EmptyState, ErrorState, Input, Json, PageHeader, Pager, Select, Spinner, Table, Td, Th } from "@/components/ui";

const AREAS = ["", "auth", "user", "organization", "member", "workflow", "execution", "agent", "integration", "webhook", "template", "package", "billing", "plan", "approval", "setting", "role", "notification_channel"];

export default function AuditPage() {
  const { org, me, can } = useSession();
  const key = useOrgKey();
  const { range, setDays } = useRange(30);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const allowed = org ? can("audit.read") : can("platform.audit.read");
  const params = { action, actor, start: range.start, end: range.end, limit: 30, offset };
  const list = useQuery({ queryKey: [key, "audit", params], queryFn: () => api<Page<any>>("/audit-logs", { params }), enabled: allowed });
  const orgName = (id: string | null) => me.organizations.find((o) => o.id === id)?.name ?? (id ? id.slice(0, 8) : "Platform");
  if (!allowed) return <EmptyState title="The audit log is not available for your role" />;
  return (
    <>
      <PageHeader title="Audit Logs" description={org ? `Sensitive actions taken in ${org.name}.` : "Sensitive actions across the platform."} />
      <Card>
        <div className="flex flex-wrap gap-2 border-b p-3">
          <Select value={action} onChange={(e) => { setAction(e.target.value); setOffset(0); }} className="h-8 w-48 text-xs" aria-label="Area">
            {AREAS.map((a) => <option key={a} value={a}>{a ? a.replace(/_/g, " ") : "All areas"}</option>)}</Select>
          <Input value={actor} onChange={(e) => { setActor(e.target.value); setOffset(0); }} placeholder="Filter by actor email" className="h-8 w-56 text-xs" aria-label="Actor" />
          <RangeSelect days={range.days} onChange={(d) => { setDays(d); setOffset(0); }} />
        </div>
        {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
          <EmptyState icon={<ScrollText className="h-5 w-5" />} title="No audit entries match these filters" />
        ) : (
          <>
            <Table>
              <thead><tr><Th>Time</Th><Th>Actor</Th><Th>Action</Th>{!org && <Th>Organization</Th>}<Th>Resource</Th><Th>IP</Th></tr></thead>
              <tbody>{list.data!.items.map((a) => (
                <Fragment key={a.id}>
                  <tr className="cursor-pointer hover:bg-raised/50" onClick={() => setOpen(open === a.id ? null : a.id)}>
                    <Td className="whitespace-nowrap text-xs text-muted">{fmtDate(a.created_at)}</Td>
                    <Td className="text-xs">{a.actor_email || "system"}</Td>
                    <Td><span className="font-mono text-xs">{a.action}</span></Td>
                    {!org && <Td className="text-xs text-muted">{orgName(a.organization_id)}</Td>}
                    <Td className="font-mono text-2xs text-muted">{a.resource_type}{a.resource_id ? ` ${a.resource_id.slice(0, 8)}` : ""}</Td>
                    <Td className="font-mono text-2xs text-muted">{a.ip ?? "-"}</Td>
                  </tr>
                  {open === a.id && <tr><Td colSpan={org ? 5 : 6} className="bg-bg/40"><Json value={a.meta} /></Td></tr>}
                </Fragment>
              ))}</tbody>
            </Table>
            <Pager total={list.data!.total} limit={30} offset={offset} onChange={setOffset} />
          </>
        )}
      </Card>
    </>
  );
}
