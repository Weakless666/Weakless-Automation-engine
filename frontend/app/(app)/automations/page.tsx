"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { NewWorkflowDialog } from "@/components/new-workflow";
import { Play, Plus, Trash2, Workflow } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtNumber, fmtRelative } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorState, InlineError, Input, NeedOrg, PageHeader, Pager, Spinner, Table, Td, Th } from "@/components/ui";

export default function AutomationsPage() {
  const { org, can } = useSession();
  const key = useOrgKey();
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState<{ id: string; text: string; exec?: string } | null>(null);
  const list = useQuery({ queryKey: [key, "workflows", q, offset], queryFn: () => api<Page<any>>("/workflows", { params: { q, limit: 25, offset } }), enabled: !!org });
  const refresh = () => qc.invalidateQueries({ queryKey: [key, "workflows"] });
  const toggle = useMutation({ mutationFn: (w: any) => api(`/workflows/${w.id}/${w.is_active ? "deactivate" : "activate"}`, { method: "POST" }), onSuccess: refresh });
  const run = useMutation({ mutationFn: (w: any) => api(`/workflows/${w.id}/execute`, { method: "POST", body: { input: {} } }),
    onSuccess: (ex: any, w: any) => { setNotice({ id: w.id, text: "Run started", exec: ex.id }); refresh(); } });
  const remove = useMutation({ mutationFn: (w: any) => api(`/workflows/${w.id}`, { method: "DELETE" }), onSuccess: refresh });

  if (!org) return <><PageHeader title="Automations" /><NeedOrg what="Automations" /></>;
  const canWrite = can("workflows.write");
  return (
    <>
      <PageHeader title="Automations" description="Workflows of this organization. A workflow runs only when it is published and active."
        actions={canWrite && <Button variant="primary" onClick={() => setCreating(true)}><Plus className="h-4 w-4" /> New workflow</Button>} />
      <Card>
        <div className="border-b p-3"><Input placeholder="Search workflows..." value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} className="max-w-xs" aria-label="Search workflows" /></div>
        {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
          <EmptyState icon={<Workflow className="h-5 w-5" />} title={q ? "No workflows match your search" : "No automations yet"}
            description={q ? undefined : "Install a template for a ready-made automation, or build one from scratch."}
            action={!q && <div className="flex gap-2"><Link href="/templates"><Button>Browse templates</Button></Link>{canWrite && <Button variant="primary" onClick={() => setCreating(true)}>New workflow</Button>}</div>} />
        ) : (
          <>
            <Table>
              <thead><tr><Th>Workflow</Th><Th>State</Th><Th className="text-right">Version</Th><Th className="text-right">Executions</Th><Th className="text-right">Last run</Th><Th /></tr></thead>
              <tbody>{list.data!.items.map((w) => (
                <tr key={w.id} className="hover:bg-raised/50">
                  <Td>
                    {canWrite ? <Link href={`/builder/${w.id}`} className="font-medium hover:text-primary">{w.name}</Link> : <span className="font-medium">{w.name}</span>}
                    {w.description && <div className="max-w-md truncate text-xs text-muted">{w.description}</div>}
                    {notice && notice.id === w.id && <div className="text-xs text-success">{notice.text} - <Link className="underline" href={`/executions/${notice.exec}`}>view execution</Link></div>}
                  </Td>
                  <Td>{w.is_active ? <Badge tone="success">Active</Badge> : w.status === "published" ? <Badge tone="neutral">Inactive</Badge> : <Badge tone="warning">Draft</Badge>}
                    {w.installed_template_id && <Badge tone="primary" className="ml-1.5">Template</Badge>}</Td>
                  <Td className="tabular text-right text-muted">{w.version ? `v${w.version}` : "-"}</Td>
                  <Td className="tabular text-right"><Link href={`/executions?workflow_id=${w.id}`} className="hover:text-primary">{fmtNumber(w.executions)}</Link></Td>
                  <Td className="text-right text-xs text-muted">{fmtRelative(w.last_execution_at)}</Td>
                  <Td>
                    <div className="flex justify-end gap-1.5">
                      {can("workflows.execute") && w.status === "published" && <Button size="sm" onClick={() => run.mutate(w)} loading={run.isPending && run.variables?.id === w.id}><Play className="h-3.5 w-3.5" /> Run</Button>}
                      {can("workflows.activate") && w.status === "published" && <Button size="sm" onClick={() => toggle.mutate(w)} loading={toggle.isPending && toggle.variables?.id === w.id}>{w.is_active ? "Deactivate" : "Activate"}</Button>}
                      {canWrite && <Button size="sm" variant="danger" aria-label={`Delete ${w.name}`} onClick={() => window.confirm(`Delete "${w.name}"? Its execution history is kept.`) && remove.mutate(w)}><Trash2 className="h-3.5 w-3.5" /></Button>}
                    </div>
                  </Td>
                </tr>
              ))}</tbody>
            </Table>
            <div className="px-4 py-2"><InlineError error={toggle.error ?? run.error ?? remove.error} /></div>
            <Pager total={list.data!.total} limit={25} offset={offset} onChange={setOffset} />
          </>
        )}
      </Card>
      <NewWorkflowDialog open={creating} onClose={() => setCreating(false)} />
    </>
  );
}
