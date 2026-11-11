"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtCompact, fmtDuration, fmtNumber, fmtRelative, fmtUsd } from "@/lib/format";
import { DurationChart, ExecutionsChart, TokensChart, type DayPoint } from "@/components/charts";
import { RangeSelect, WorkflowSelect, useRange } from "@/components/filters";
import { Card, CardHeader, EmptyState, ErrorState, PageHeader, Spinner, Stat, StatusBadge, Table, Td, Th } from "@/components/ui";

export default function AnalyticsPage() {
  const { org, can } = useSession();
  const key = useOrgKey();
  const { range, setDays } = useRange(30);
  const [workflow, setWorkflow] = useState("");
  const allowed = org ? can("analytics.read") : can("platform.analytics.read");
  const base = { start: range.start, end: range.end };
  const params = { ...base, workflow_id: org ? workflow : "" };
  const overview = useQuery({ queryKey: [key, "overview", params], queryFn: () => api("/analytics/overview", { params }), enabled: allowed });
  const series = useQuery({ queryKey: [key, "timeseries", params], queryFn: () => api<{ items: DayPoint[] }>("/analytics/timeseries", { params }), enabled: allowed });
  const workflows = useQuery({ queryKey: [key, "an-workflows", base], queryFn: () => api<{ items: any[] }>("/analytics/workflows", { params: base }), enabled: allowed });
  const ai = useQuery({ queryKey: [key, "an-ai", base], queryFn: () => api("/analytics/ai", { params: base }), enabled: allowed });
  const integrations = useQuery({ queryKey: [key, "an-integrations"], queryFn: () => api("/analytics/integrations"), enabled: allowed });
  const system = useQuery({ queryKey: ["an-system"], queryFn: () => api("/analytics/system", { org: false }), enabled: can("platform.analytics.read"), refetchInterval: 15_000 });

  if (!allowed) return <EmptyState title="Analytics are not available for your role" />;
  const d = overview.data;
  const empty = (series.data?.items ?? []).every((p) => p.total === 0);
  return (
    <>
      <PageHeader title="Analytics" description={org ? `Performance of ${org.name}'s automations.` : "Performance across all organizations."}
        actions={<><WorkflowSelect value={workflow} onChange={setWorkflow} /><RangeSelect days={range.days} onChange={setDays} /></>} />
      {overview.isLoading ? <Spinner /> : overview.error ? <ErrorState error={overview.error} onRetry={() => overview.refetch()} /> : d && (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Stat label="Success rate" value={d.executions.success_rate === null ? "-" : `${d.executions.success_rate}%`} sub={`${fmtNumber(d.executions.completed)} of ${fmtNumber(d.executions.completed + d.executions.failed)} finished runs`} tone={d.executions.success_rate !== null && d.executions.success_rate < 90 ? "warning" : undefined} />
            <Stat label="Failure rate" value={d.executions.failure_rate === null ? "-" : `${d.executions.failure_rate}%`} sub={`${fmtNumber(d.executions.failed)} failed`} tone={d.executions.failed ? "danger" : undefined} />
            <Stat label="Average duration" value={fmtDuration(d.executions.avg_duration_ms)} sub={`${fmtNumber(d.executions.total)} executions`} />
            <Stat label="AI tokens" value={fmtCompact(d.ai.total_tokens)} sub={`${fmtUsd(d.ai.estimated_cost_usd)} estimated`} />
          </div>
          <Card><CardHeader title="Executions" description="Completed and failed runs per day" />
            <div className="px-2 py-3">{series.isLoading ? <Spinner /> : empty ? <p className="py-14 text-center text-sm text-muted">No executions in this period.</p> : <ExecutionsChart data={series.data!.items} />}</div></Card>
          <div className="grid gap-5 lg:grid-cols-2">
            <Card><CardHeader title="Average duration" /><div className="px-2 py-3">{series.isLoading ? <Spinner /> : empty ? <p className="py-14 text-center text-sm text-muted">No data.</p> : <DurationChart data={series.data!.items} />}</div></Card>
            <Card><CardHeader title="AI token consumption" /><div className="px-2 py-3">{series.isLoading ? <Spinner /> : (series.data?.items ?? []).every((p) => p.ai_tokens === 0) ? <p className="py-14 text-center text-sm text-muted">No AI usage in this period.</p> : <TokensChart data={series.data!.items} />}</div></Card>
          </div>
          <Card><CardHeader title="By automation" />
            {workflows.isLoading ? <Spinner /> : workflows.error ? <div className="p-4"><ErrorState error={workflows.error} /></div> : workflows.data!.items.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">No executions in this period.</p> : (
              <Table><thead><tr><Th>Workflow</Th><Th className="text-right">Executions</Th><Th className="text-right">Completed</Th><Th className="text-right">Failed</Th><Th className="text-right">Success</Th><Th className="text-right">Avg duration</Th><Th className="text-right">Last run</Th></tr></thead>
                <tbody>{workflows.data!.items.map((w) => (
                  <tr key={w.workflow_id}><Td className="font-medium"><Link href={`/executions?workflow_id=${w.workflow_id}`} className="hover:text-primary">{w.workflow_name || w.workflow_id.slice(0, 8)}</Link></Td>
                    <Td className="tabular text-right">{fmtNumber(w.executions)}</Td><Td className="tabular text-right text-success">{fmtNumber(w.completed)}</Td>
                    <Td className={`tabular text-right ${w.failed ? "text-danger" : "text-muted"}`}>{fmtNumber(w.failed)}</Td>
                    <Td className="tabular text-right">{w.success_rate === null ? "-" : `${w.success_rate}%`}</Td><Td className="tabular text-right text-muted">{fmtDuration(w.avg_duration_ms)}</Td>
                    <Td className="text-right text-xs text-muted">{fmtRelative(w.last_execution_at)}</Td></tr>))}</tbody></Table>)}
          </Card>
          <div className="grid gap-5 lg:grid-cols-2">
            <Card><CardHeader title="AI usage by model" description={ai.data?.summary.unpriced_requests ? `${ai.data.summary.unpriced_requests} runs used models without a configured price and are not in the cost estimate.` : "Cost is estimated from the configured model prices."} />
              {ai.isLoading ? <Spinner /> : ai.error ? <div className="p-4"><ErrorState error={ai.error} /></div> : ai.data.by_model.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">No AI runs in this period.</p> : (
                <Table><thead><tr><Th>Model</Th><Th className="text-right">Runs</Th><Th className="text-right">Input</Th><Th className="text-right">Output</Th><Th className="text-right">Est. cost</Th></tr></thead>
                  <tbody>{ai.data.by_model.map((m: any) => (
                    <tr key={`${m.provider}/${m.model}`}><Td><span className="font-mono text-xs">{m.model}</span><div className="text-xs text-muted">{m.provider}{m.failed ? ` - ${m.failed} failed` : ""}</div></Td>
                      <Td className="tabular text-right">{fmtNumber(m.runs)}</Td><Td className="tabular text-right">{fmtCompact(m.input_tokens)}</Td><Td className="tabular text-right">{fmtCompact(m.output_tokens)}</Td>
                      <Td className="tabular text-right">{m.estimated_cost_usd === null ? <span className="text-muted">unpriced</span> : fmtUsd(m.estimated_cost_usd)}</Td></tr>))}</tbody></Table>)}
            </Card>
            <Card><CardHeader title="Integration health" description={integrations.data?.summary.uptime_percent != null ? `${integrations.data.summary.uptime_percent}% of verified connections are healthy` : undefined} />
              {integrations.isLoading ? <Spinner /> : integrations.error ? <div className="p-4"><ErrorState error={integrations.error} /></div> : integrations.data.items.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">No integrations.</p> : (
                <Table><tbody>{integrations.data.items.slice(0, 12).map((i: any) => (
                  <tr key={i.id}><Td><span className="font-medium">{i.name}</span><div className="text-xs text-muted">{i.provider_key}</div></Td><Td><StatusBadge status={i.status} /></Td>
                    <Td className="max-w-[14rem] truncate text-right text-xs text-muted">{i.last_error ?? `checked ${fmtRelative(i.last_checked_at)}`}</Td></tr>))}</tbody></Table>)}
            </Card>
          </div>
          {system.data && (
            <Card><CardHeader title="System" description="Platform services" />
              <dl className="grid grid-cols-2 gap-px bg-line/60 text-sm md:grid-cols-5">
                {[["Database", system.data.database], ["Queue", system.data.queue_status], ["Ready jobs", fmtNumber(system.data.queue.ready)], ["Queue latency", `${system.data.queue.oldest_ready_age_seconds}s`], ["Dead letters", fmtNumber(system.data.queue.dead)]].map(([l, v]) => (
                  <div key={l as string} className="bg-surface px-4 py-3"><dt className="text-xs text-muted">{l}</dt><dd className={`mt-0.5 font-medium ${v === "ok" ? "text-success" : v === "error" || v === "stalled" ? "text-danger" : v === "degraded" ? "text-warning" : ""}`}>{v}</dd></div>))}
              </dl></Card>
          )}
        </div>
      )}
    </>
  );
}
