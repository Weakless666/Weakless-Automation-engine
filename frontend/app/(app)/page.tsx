"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtCompact, fmtDuration, fmtNumber, fmtRelative, fmtUsd } from "@/lib/format";
import { ExecutionsChart, type DayPoint } from "@/components/charts";
import { RangeSelect, StatusSelect, WorkflowSelect, useRange } from "@/components/filters";
import { Card, CardHeader, EmptyState, ErrorState, PageHeader, Spinner, Stat, StatusBadge, Table, Td, Th } from "@/components/ui";

function ExecList({ rows, showOrg, empty }: { rows: any[]; showOrg: boolean; empty: string }) {
  if (!rows.length) return <p className="px-4 py-8 text-center text-sm text-muted">{empty}</p>;
  return (
    <Table>
      <tbody>
        {rows.map((e) => (
          <tr key={e.id} className="hover:bg-raised/50">
            <Td>
              <Link href={`/executions/${e.id}`} className="font-medium hover:text-primary">{e.workflow_name || "Workflow"}</Link>
              {showOrg && <div className="text-xs text-muted">{e.organization_name}</div>}
              {e.error && <div className="max-w-md truncate text-xs text-danger">{e.error}</div>}
            </Td>
            <Td className="w-28"><StatusBadge status={e.status} /></Td>
            <Td className="tabular w-24 text-right text-xs text-muted">{fmtDuration(e.duration_ms)}</Td>
            <Td className="w-28 text-right text-xs text-muted">{fmtRelative(e.created_at)}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

export default function OverviewPage() {
  const { org, isStaff, can } = useSession();
  const key = useOrgKey();
  const { range, setDays } = useRange(30);
  const [workflow, setWorkflow] = useState("");
  const [status, setStatus] = useState("");
  const params = { start: range.start, end: range.end, workflow_id: org ? workflow : "", status };
  const allowed = org ? can("analytics.read") : can("platform.analytics.read");

  const overview = useQuery({ queryKey: [key, "overview", params], queryFn: () => api("/analytics/overview", { params }), enabled: allowed, refetchInterval: 30_000 });
  const series = useQuery({ queryKey: [key, "timeseries", params], queryFn: () => api<{ items: DayPoint[] }>("/analytics/timeseries", { params }), enabled: allowed });

  if (!allowed) return <EmptyState title="Nothing to show yet" description="Your role does not include analytics. Use the navigation to open the areas you have access to." />;
  const d = overview.data;
  const platform = isStaff && !org;

  return (
    <>
      <PageHeader
        title={org ? org.name : "Control Center"}
        description={platform ? "Live state of every organization on the platform." : "Automation activity for this organization."}
        actions={<><WorkflowSelect value={workflow} onChange={setWorkflow} /><StatusSelect value={status} onChange={setStatus} /><RangeSelect days={range.days} onChange={setDays} /></>}
      />
      {overview.isLoading ? <Spinner /> : overview.error ? <ErrorState error={overview.error} onRetry={() => overview.refetch()} /> : d && (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {platform && <Stat label="Organizations" value={fmtNumber(d.organizations.total)} sub={`${fmtNumber(d.organizations.active)} active - ${fmtNumber(d.clients.registered_users)} client users`} />}
            <Stat label="Active workflows" value={fmtNumber(d.workflows.active)} sub={`${fmtNumber(d.workflows.total)} total`} />
            <Stat label="Executions" value={fmtNumber(d.executions.total)} sub={d.executions.success_rate === null ? "No finished runs in range" : `${d.executions.success_rate}% success`} />
            <Stat label="Successful" value={fmtNumber(d.executions.completed)} tone="success" sub={`Avg ${fmtDuration(d.executions.avg_duration_ms)}`} />
            <Stat label="Failed" value={fmtNumber(d.executions.failed)} tone={d.executions.failed ? "danger" : undefined} sub={d.executions.failure_rate === null ? "-" : `${d.executions.failure_rate}% failure rate`} />
            <Stat label="Running / waiting" value={fmtNumber(d.executions.running)} sub={`${fmtNumber(d.executions.cancelled)} cancelled`} />
            <Stat label="AI requests" value={fmtNumber(d.ai.requests)} sub={`${fmtCompact(d.ai.total_tokens)} tokens`} />
            <Stat label="Estimated AI cost" value={fmtUsd(d.ai.estimated_cost_usd)} sub={d.ai.unpriced_requests ? `${d.ai.unpriced_requests} runs on unpriced models excluded` : "From configured model prices"} />
          </div>

          <Card>
            <CardHeader title="Usage trend" description="Executions per day (UTC)" />
            <div className="px-2 py-3">
              {series.isLoading ? <Spinner /> : series.error ? <ErrorState error={series.error} /> : (series.data?.items ?? []).every((p) => p.total === 0)
                ? <p className="py-16 text-center text-sm text-muted">No executions in this period.</p>
                : <ExecutionsChart data={series.data!.items} />}
            </div>
          </Card>

          <div className="grid gap-5 lg:grid-cols-3">
            <Card>
              <CardHeader title="Integration health" />
              <dl className="divide-y divide-line/60 text-sm">
                {[["Connected", d.integrations.connected, "text-success"], ["Errors", d.integrations.error, d.integrations.error ? "text-danger" : ""],
                  ["Awaiting credentials", d.integrations.awaiting_credentials, d.integrations.awaiting_credentials ? "text-warning" : ""], ["Pending authorization", d.integrations.pending, ""]].map(([l, v, c]) => (
                  <div key={l as string} className="flex justify-between px-4 py-2.5"><dt className="text-muted">{l}</dt><dd className={`tabular font-medium ${c}`}>{fmtNumber(v as number)}</dd></div>
                ))}
              </dl>
            </Card>
            {platform && (
              <Card>
                <CardHeader title="Queue health" description="Durable job queue" />
                <dl className="divide-y divide-line/60 text-sm">
                  {[["Ready", d.queue.ready, ""], ["Scheduled / delayed", d.queue.scheduled, ""], ["In progress", d.queue.claimed, ""],
                    ["Dead letters", d.queue.dead, d.queue.dead ? "text-danger" : ""], ["Oldest ready job", `${d.queue.oldest_ready_age_seconds}s`, d.queue.oldest_ready_age_seconds > 60 ? "text-warning" : ""]].map(([l, v, c]) => (
                    <div key={l as string} className="flex justify-between px-4 py-2.5"><dt className="text-muted">{l}</dt><dd className={`tabular font-medium ${c}`}>{typeof v === "number" ? fmtNumber(v) : v}</dd></div>
                  ))}
                </dl>
              </Card>
            )}
            {platform && (
              <Card className="lg:col-span-1">
                <CardHeader title="Client activity" description="Most active organizations in range" />
                {d.client_activity.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">No client activity yet.</p> : (
                  <Table><tbody>{d.client_activity.map((c: any) => (
                    <tr key={c.organization_id}><Td className="font-medium">{c.organization_name}</Td>
                      <Td className="tabular text-right">{fmtNumber(c.executions)}</Td>
                      <Td className="tabular w-16 text-right text-xs text-danger">{c.failed ? `${c.failed} failed` : ""}</Td></tr>
                  ))}</tbody></Table>
                )}
              </Card>
            )}
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card><CardHeader title="Recent activity" actions={<Link href="/executions" className="text-xs text-primary hover:underline">View all</Link>} />
              <ExecList rows={d.recent_activity} showOrg={platform} empty="No executions yet. Activate an automation to see activity here." /></Card>
            <Card><CardHeader title="Recent errors" />
              <ExecList rows={d.recent_errors} showOrg={platform} empty="No failures in this period." /></Card>
          </div>
        </div>
      )}
    </>
  );
}
