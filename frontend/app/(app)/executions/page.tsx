"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Activity, ShieldCheck } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDate, fmtDuration, fmtRelative } from "@/lib/format";
import { RangeSelect, StatusSelect, WorkflowSelect, useRange } from "@/components/filters";
import { Button, Card, Checkbox, Code, Dialog, EmptyState, ErrorState, Field, InlineError, PageHeader, Pager, Spinner, StatusBadge, Table, Tabs, Td, Textarea, Th } from "@/components/ui";

function Approvals() {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const [status, setStatus] = useState("PENDING");
  const [deciding, setDeciding] = useState<{ item: any; approved: boolean } | null>(null);
  const [note, setNote] = useState("");
  const list = useQuery({ queryKey: [key, "approvals", status], queryFn: () => api<Page<any>>("/approvals", { params: { status, limit: 50 } }), refetchInterval: 15_000 });
  const decide = useMutation({
    mutationFn: () => api(`/approvals/${deciding!.item.id}/decide`, { method: "POST", body: { approved: deciding!.approved, note } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [key, "approvals"] }); setDeciding(null); setNote(""); },
  });
  return (
    <Card>
      <div className="flex gap-2 border-b p-3">
        {["PENDING", "APPROVED", "REJECTED", "EXPIRED"].map((s) => <Button key={s} size="sm" variant={status === s ? "primary" : "secondary"} onClick={() => setStatus(s)}>{s.charAt(0) + s.slice(1).toLowerCase()}</Button>)}
      </div>
      {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} /></div> : list.data!.items.length === 0 ? (
        <EmptyState icon={<ShieldCheck className="h-5 w-5" />} title={status === "PENDING" ? "Nothing is waiting for approval" : `No ${status.toLowerCase()} approvals`}
          description="Workflow approval steps and high-impact AI agent actions pause here until a person decides." />
      ) : (
        <ul className="divide-y divide-line/60">{list.data!.items.map((a) => (
          <li key={a.id} className="p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2"><StatusBadge status={a.status} /><span className="font-medium">{a.title}</span></div>
                <div className="mt-1 text-xs text-muted">{a.kind === "agent_tool" ? "AI agent action" : "Workflow step"} - requested {fmtRelative(a.created_at)}{a.expires_at && a.status === "PENDING" ? ` - expires ${fmtRelative(a.expires_at)}` : ""}
                  {a.execution_id && <> - <Link href={`/executions/${a.execution_id}`} className="text-primary hover:underline">execution</Link></>}</div>
                {a.decision_note && <div className="mt-1 text-xs text-muted">Note: {a.decision_note}</div>}
              </div>
              {a.status === "PENDING" && can("approvals.decide") && (
                <div className="flex gap-2"><Button size="sm" variant="danger" onClick={() => setDeciding({ item: a, approved: false })}>Reject</Button>
                  <Button size="sm" variant="primary" onClick={() => setDeciding({ item: a, approved: true })}>Approve</Button></div>
              )}
            </div>
            {a.summary && <Code className="mt-3 max-h-48">{a.summary}</Code>}
          </li>
        ))}</ul>
      )}
      <Dialog open={!!deciding} onClose={() => setDeciding(null)} title={deciding?.approved ? "Approve this action?" : "Reject this action?"} description={deciding?.item.title}
        footer={<><Button onClick={() => setDeciding(null)}>Cancel</Button><Button variant={deciding?.approved ? "primary" : "danger"} loading={decide.isPending} onClick={() => decide.mutate()}>{deciding?.approved ? "Approve" : "Reject"}</Button></>}>
        <Field label="Note (optional)" hint="Shown to the requester and, for agent actions, passed to the agent."><Textarea value={note} onChange={(e) => setNote(e.target.value)} /></Field>
        <InlineError error={decide.error} />
      </Dialog>
    </Card>
  );
}

function DeadLetters() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["dead-letters"], queryFn: () => api<Page<any>>("/dead-letters", { org: false, params: { limit: 50 } }) });
  const requeue = useMutation({ mutationFn: (id: string) => api(`/dead-letters/${id}/requeue`, { method: "POST", org: false }), onSuccess: () => qc.invalidateQueries({ queryKey: ["dead-letters"] }) });
  return (
    <Card>
      {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} /></div> : list.data!.items.length === 0 ? (
        <EmptyState title="The dead-letter queue is empty" description="Jobs land here only after repeated infrastructure failures. Their executions are marked failed." />
      ) : (
        <Table>
          <thead><tr><Th>Job</Th><Th>Attempts</Th><Th>Last error</Th><Th>Failed</Th><Th /></tr></thead>
          <tbody>{list.data!.items.map((j) => (
            <tr key={j.id}><Td className="font-mono text-xs">{j.execution_id ? <Link className="hover:text-primary" href={`/executions/${j.execution_id}`}>{j.execution_id.slice(0, 8)}</Link> : j.kind}</Td>
              <Td className="tabular">{j.attempts}</Td><Td className="max-w-md truncate text-xs text-danger">{j.last_error}</Td><Td className="text-xs text-muted">{fmtDate(j.finished_at)}</Td>
              <Td className="text-right">{j.execution_id && <Button size="sm" loading={requeue.isPending && requeue.variables === j.id} onClick={() => requeue.mutate(j.id)}>Run again</Button>}</Td></tr>
          ))}</tbody>
        </Table>
      )}
      <div className="px-4 py-2"><InlineError error={requeue.error} /></div>
    </Card>
  );
}

function Runs() {
  const { org, isStaff } = useSession();
  const key = useOrgKey();
  const search = useSearchParams();
  const { range, setDays } = useRange(30);
  const [workflow, setWorkflow] = useState(search.get("workflow_id") ?? "");
  const [status, setStatus] = useState("");
  const [tests, setTests] = useState(false);
  const [offset, setOffset] = useState(0);
  const params = { status, workflow_id: workflow, start: range.start, end: range.end, include_tests: tests, limit: 25, offset };
  const list = useQuery({ queryKey: [key, "executions", params], queryFn: () => api<Page<any>>("/executions", { params }), refetchInterval: 10_000 });
  const reset = () => setOffset(0);
  return (
    <Card>
      <div className="flex flex-wrap items-center gap-2 border-b p-3">
        <WorkflowSelect value={workflow} onChange={(v) => { setWorkflow(v); reset(); }} />
        <StatusSelect value={status} onChange={(v) => { setStatus(v); reset(); }} />
        <RangeSelect days={range.days} onChange={(d) => { setDays(d); reset(); }} />
        <Checkbox label="Include test runs" checked={tests} onChange={(v) => { setTests(v); reset(); }} />
      </div>
      {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
        <EmptyState icon={<Activity className="h-5 w-5" />} title="No executions match these filters" description="Executions appear here as soon as a trigger fires or a workflow is run manually." />
      ) : (
        <>
          <Table>
            <thead><tr><Th>Execution</Th><Th>Workflow</Th>{isStaff && !org && <Th>Organization</Th>}<Th>Status</Th><Th>Trigger</Th><Th className="text-right">Duration</Th><Th className="text-right">Started</Th></tr></thead>
            <tbody>{list.data!.items.map((e) => (
              <tr key={e.id} className="hover:bg-raised/50">
                <Td><Link href={`/executions/${e.id}`} className="font-mono text-xs hover:text-primary">{e.id.slice(0, 8)}</Link>{e.is_test && <span className="ml-1.5 text-2xs uppercase text-muted">test</span>}</Td>
                <Td><span className="font-medium">{e.workflow_name ?? "-"}</span>{e.error?.message && <div className="max-w-sm truncate text-xs text-danger">{e.error.message}</div>}</Td>
                {isStaff && !org && <Td className="text-xs text-muted">{e.organization_name}</Td>}
                <Td><StatusBadge status={e.status} /></Td>
                <Td className="text-xs text-muted">{e.trigger_type.replace(/_/g, " ")}</Td>
                <Td className="tabular text-right text-xs text-muted">{fmtDuration(e.duration_ms)}</Td>
                <Td className="text-right text-xs text-muted" title={fmtDate(e.created_at)}>{fmtRelative(e.created_at)}</Td>
              </tr>
            ))}</tbody>
          </Table>
          <Pager total={list.data!.total} limit={25} offset={offset} onChange={setOffset} />
        </>
      )}
    </Card>
  );
}

function ExecutionsInner() {
  const { org, can } = useSession();
  const [tab, setTab] = useState<"runs" | "approvals" | "dead">("runs");
  const tabs = [{ key: "runs" as const, label: "Runs" }, ...(org ? [{ key: "approvals" as const, label: "Approvals" }] : []),
    ...(can("platform.analytics.read") ? [{ key: "dead" as const, label: "Dead letters" }] : [])];
  return (
    <>
      <PageHeader title="Executions" description="Every run is traceable by its execution id, with a step-by-step log." />
      <Tabs tabs={tabs} value={tabs.some((t) => t.key === tab) ? tab : "runs"} onChange={setTab} />
      {tab === "approvals" && org ? <Approvals /> : tab === "dead" ? <DeadLetters /> : <Runs />}
    </>
  );
}

export default function ExecutionsPage() {
  return <Suspense fallback={<Spinner />}><ExecutionsInner /></Suspense>;
}
