"use client";

import Link from "next/link";
import { use, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, RefreshCw, Square } from "lucide-react";
import { api } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDate, fmtDuration } from "@/lib/format";
import { Button, Card, CardHeader, Code, ErrorState, InlineError, Json, Notice, PageHeader, Spinner, StatusBadge } from "@/components/ui";

const LIVE = ["PENDING", "QUEUED", "RUNNING", "RETRYING", "WAITING"];

function Step({ s }: { s: any }) {
  const [open, setOpen] = useState(s.status === "FAILED");
  return (
    <li className="border-b border-line/60 last:border-0">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-raised/50">
        {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-muted" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-muted" />}
        <span className="tabular w-6 text-xs text-muted">{s.sequence}</span>
        <span className="w-24 shrink-0"><StatusBadge status={s.status} /></span>
        <span className="min-w-0 flex-1"><span className="font-medium">{s.node_name || s.node_id}</span>
          <span className="ml-2 font-mono text-xs text-muted">{s.node_type}{s.iteration ? ` [${s.iteration}]` : ""}{s.attempt > 1 ? ` attempt ${s.attempt}` : ""}</span></span>
        <span className="tabular text-xs text-muted">{fmtDuration(s.duration_ms)}</span>
      </button>
      {open && (
        <div className="grid gap-3 bg-bg/40 px-4 pb-4 pt-1 lg:grid-cols-2">
          {s.error && <div className="lg:col-span-2"><div className="mb-1 text-2xs font-medium uppercase tracking-wider text-danger">Error</div><Json value={s.error} /></div>}
          <div><div className="mb-1 text-2xs font-medium uppercase tracking-wider text-muted">Input (resolved configuration)</div><Json value={s.input} /></div>
          <div><div className="mb-1 text-2xs font-medium uppercase tracking-wider text-muted">Output</div><Json value={s.output} /></div>
        </div>
      )}
    </li>
  );
}

export default function ExecutionPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const key = useOrgKey();
  const router = useRouter();
  const { can } = useSession();
  const q = useQuery({ queryKey: [key, "execution", id], queryFn: () => api(`/executions/${id}`),
    refetchInterval: (query) => (query.state.data && LIVE.includes((query.state.data as any).status) ? 2000 : false) });
  const cancel = useMutation({ mutationFn: () => api(`/executions/${id}/cancel`, { method: "POST" }), onSuccess: () => q.refetch() });
  const retry = useMutation({ mutationFn: () => api(`/executions/${id}/retry`, { method: "POST" }), onSuccess: (e: any) => router.push(`/executions/${e.id}`) });
  const resume = useMutation({ mutationFn: (node: string) => api(`/executions/${id}/resume`, { method: "POST", body: { node_id: node, data: {} } }), onSuccess: () => q.refetch() });

  if (q.isLoading) return <Spinner />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const e = q.data;
  const manage = can("executions.manage");
  return (
    <>
      <div className="mb-2 text-xs text-muted"><Link href="/executions" className="hover:text-fg">Executions</Link> / <span className="font-mono">{e.id}</span></div>
      <PageHeader title={e.workflow_name ?? "Execution"}
        description={<span className="flex flex-wrap items-center gap-2"><StatusBadge status={e.status} />{e.is_test && <span className="text-2xs uppercase">Test run</span>}
          <span>Trigger: {e.trigger_type.replace(/_/g, " ")}</span>{e.version && <span>- version {e.version}</span>}<span>- started {fmtDate(e.started_at ?? e.created_at)}</span><span>- {fmtDuration(e.duration_ms)}</span></span>}
        actions={manage && <>
          {LIVE.includes(e.status) && <Button variant="danger" loading={cancel.isPending} onClick={() => window.confirm("Cancel this execution?") && cancel.mutate()}><Square className="h-3.5 w-3.5" /> Cancel</Button>}
          {["FAILED", "CANCELLED"].includes(e.status) && <Button variant="primary" loading={retry.isPending}
            onClick={() => window.confirm("Run this workflow again with the same trigger data? Steps that already completed will run again.") && retry.mutate()}><RefreshCw className="h-3.5 w-3.5" /> Retry</Button>}
        </>} />
      <div className="space-y-5">
        <InlineError error={cancel.error ?? retry.error ?? resume.error} />
        {e.error && <Notice tone="warning"><strong>{e.error.node_name ?? e.error.node_id ?? "Execution"}</strong>: {e.error.message} <span className="font-mono opacity-70">({e.error.code})</span></Notice>}
        {e.retry_of_id && <Notice>This is a retry of <Link className="underline" href={`/executions/${e.retry_of_id}`}>{e.retry_of_id.slice(0, 8)}</Link>.</Notice>}
        {e.waiting.length > 0 && (
          <Card><CardHeader title="Paused" description="The execution continues automatically when the wait ends." />
            <ul className="divide-y divide-line/60">{e.waiting.map((w: any) => (
              <li key={w.node_id} className="flex items-center justify-between px-4 py-2.5 text-sm">
                <span><span className="font-mono text-xs">{w.node_id}</span> - {w.kind === "delay" ? `delay until ${fmtDate(w.resume_at)}` : w.kind === "approval" ? "waiting for approval" : w.kind === "agent" ? "AI agent waiting for approval" : "waiting for an external signal"}</span>
                {w.kind === "external" && manage && <Button size="sm" loading={resume.isPending} onClick={() => resume.mutate(w.node_id)}>Resume now</Button>}
                {(w.kind === "approval" || w.kind === "agent") && <Link href="/executions" className="text-xs text-primary hover:underline">Open approvals</Link>}
              </li>
            ))}</ul></Card>
        )}
        <Card>
          <CardHeader title="Steps" description={`${e.steps.length} step records in execution order`} />
          {e.steps.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">{LIVE.includes(e.status) ? "Waiting for a worker to pick up this execution..." : "No steps were recorded."}</p>
            : <ul>{e.steps.map((s: any) => <Step key={s.id} s={s} />)}</ul>}
        </Card>
        <div className="grid gap-5 lg:grid-cols-2">
          <Card><CardHeader title="Trigger payload" /><div className="p-4"><Json value={e.trigger_payload} /></div></Card>
          <Card><CardHeader title="Result" description="Output of the final steps" /><div className="p-4"><Json value={e.output} /></div></Card>
        </div>
        {e.documents.length > 0 && (
          <Card><CardHeader title="Generated documents" />
            <div className="space-y-4 p-4">{e.documents.map((d: any) => <div key={d.id}><div className="mb-1 text-sm font-medium">{d.name} <span className="text-xs text-muted">({d.content_type})</span></div><Code>{d.content}</Code></div>)}</div></Card>
        )}
      </div>
    </>
  );
}
