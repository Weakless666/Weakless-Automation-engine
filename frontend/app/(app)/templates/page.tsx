"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { LayoutTemplate } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDay, titleCase } from "@/lib/format";
import { DefinitionOutline, TemplateConfig } from "@/components/template-config";
import { Badge, Button, Card, Dialog, EmptyState, ErrorState, InlineError, Input, Notice, PageHeader, Select, Spinner, StatusBadge, Table, Tabs, Td, Th } from "@/components/ui";

function useCanInstall() {
  const { org, can, isStaff } = useSession();
  if (!org) return { ok: false, reason: "Select an organization to install templates." };
  if (!can("templates.install")) return { ok: false, reason: "Your role cannot install templates." };
  if (!isStaff && org.mode !== "saas") return { ok: false, reason: "Templates for this organization are installed by your Weakless team." };
  return { ok: true, reason: "" };
}

function Preview({ id, onClose }: { id: string | null; onClose: () => void }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const router = useRouter();
  const { can } = useSession();
  const install = useCanInstall();
  const [config, setConfig] = useState<Record<string, any>>({});
  const t = useQuery({ queryKey: ["template", id], queryFn: () => api(`/templates/${id}`, { org: false }), enabled: !!id });
  useEffect(() => setConfig({}), [id]);
  const run = useMutation({
    mutationFn: () => api(`/templates/${id}/install`, { method: "POST", body: { config } }),
    onSuccess: (i: any) => { qc.invalidateQueries({ queryKey: [key] }); onClose(); router.push(can("workflows.write") ? `/builder/${i.workflow_id}` : "/automations"); },
  });
  return (
    <Dialog open={!!id} onClose={onClose} wide title={t.data?.name ?? "Template"} description={t.data?.description}
      footer={<><Button onClick={onClose}>Close</Button>{install.ok && <Button variant="primary" loading={run.isPending} disabled={!t.data} onClick={() => run.mutate()}>Install into this organization</Button>}</>}>
      {t.isLoading ? <Spinner /> : t.error ? <ErrorState error={t.error} /> : t.data && (
        <>
          <div className="flex flex-wrap gap-1.5">
            <Badge tone="primary">{t.data.category}</Badge>{t.data.industry && <Badge>{titleCase(t.data.industry)}</Badge>}<Badge>Version {t.data.version}</Badge>
            {t.data.required_integrations.map((r: string) => <Badge key={r} tone="info">Needs {r}</Badge>)}
            {t.data.required_credentials.map((r: string) => <Badge key={r} tone="warning">{r}</Badge>)}
          </div>
          <div><h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Steps</h3><DefinitionOutline definition={t.data.definition} /></div>
          <div><h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Setup</h3><p className="whitespace-pre-line text-sm text-muted">{t.data.setup_instructions}</p></div>
          {install.ok ? (
            <div><h3 className="mb-2 text-xs font-medium uppercase tracking-wider text-muted">Configuration</h3>
              <p className="mb-3 text-xs text-muted">You can install now and finish the configuration later; the workflow stays a draft until you publish it.</p>
              <TemplateConfig schema={t.data.config_schema} value={config} onChange={setConfig} /></div>
          ) : <Notice tone="warning">{install.reason}</Notice>}
          <InlineError error={run.error} />
        </>
      )}
    </Dialog>
  );
}

function Library() {
  const [category, setCategory] = useState("");
  const [q, setQ] = useState("");
  const [preview, setPreview] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["templates", category, q], queryFn: () => api<Page<any> & { categories: string[] }>("/templates", { org: false, params: { category, q, limit: 200 } }) });
  return (
    <>
      <div className="mb-4 flex flex-wrap gap-2">
        <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search templates..." className="h-8 w-56 text-xs" aria-label="Search templates" />
        <Select value={category} onChange={(e) => setCategory(e.target.value)} className="h-8 w-56 text-xs" aria-label="Category">
          <option value="">All categories</option>{(list.data?.categories ?? []).map((c) => <option key={c} value={c}>{c}</option>)}</Select>
      </div>
      {list.isLoading ? <Spinner /> : list.error ? <ErrorState error={list.error} onRetry={() => list.refetch()} /> : list.data!.items.length === 0 ? (
        <Card><EmptyState icon={<LayoutTemplate className="h-5 w-5" />} title="No templates match" description="Try a different category or search term." /></Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {list.data!.items.map((t) => (
            <button key={t.id} type="button" onClick={() => setPreview(t.id)} className="flex flex-col rounded-lg border bg-surface p-4 text-left transition-colors hover:border-primary/50">
              <div className="flex items-start justify-between gap-2"><span className="font-medium">{t.name}</span>{t.status !== "published" && <StatusBadge status={t.status} />}</div>
              <p className="mt-1 line-clamp-3 flex-1 text-xs leading-relaxed text-muted">{t.description}</p>
              <div className="mt-3 flex flex-wrap gap-1.5"><Badge tone="primary">{t.category}</Badge>{t.required_integrations.slice(0, 3).map((r: string) => <Badge key={r}>{r}</Badge>)}</div>
            </button>
          ))}
        </div>
      )}
      <Preview id={preview} onClose={() => setPreview(null)} />
    </>
  );
}

function Installed() {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const install = useCanInstall();
  const [editing, setEditing] = useState<any | null>(null);
  const [config, setConfig] = useState<Record<string, any>>({});
  const list = useQuery({ queryKey: [key, "installed"], queryFn: () => api<Page<any>>("/installed-templates", { params: { limit: 100 } }) });
  const refresh = () => qc.invalidateQueries({ queryKey: [key, "installed"] });
  const save = useMutation({ mutationFn: () => api(`/installed-templates/${editing.id}/config`, { method: "PUT", body: { config } }), onSuccess: () => { refresh(); setEditing(null); } });
  const update = useMutation({ mutationFn: (id: string) => api(`/installed-templates/${id}/update`, { method: "POST" }), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => api(`/installed-templates/${id}`, { method: "DELETE" }), onSuccess: refresh });
  if (list.isLoading) return <Spinner />;
  if (list.error) return <ErrorState error={list.error} onRetry={() => list.refetch()} />;
  return (
    <Card>
      {list.data!.items.length === 0 ? <EmptyState title="No templates installed" description="Templates you install appear here with their configuration and update status." /> : (
        <Table>
          <thead><tr><Th>Template</Th><Th>Workflow</Th><Th>Configuration</Th><Th>Version</Th><Th>Installed</Th><Th /></tr></thead>
          <tbody>{list.data!.items.map((i) => (
            <tr key={i.id}>
              <Td><span className="font-medium">{i.template_name}</span><div className="text-xs text-muted">{i.category}</div></Td>
              <Td>{!i.workflow_id ? <span className="text-xs text-muted">Workflow deleted</span> : <>
                {i.workflow_active ? <Badge tone="success">Active</Badge> : i.workflow_published ? <Badge>Inactive</Badge> : <Badge tone="warning">Draft</Badge>}
                {can("workflows.write") && <Link href={`/builder/${i.workflow_id}`} className="ml-2 text-xs text-primary hover:underline">Open</Link>}</>}</Td>
              <Td className="text-xs">{i.missing_config.length ? <span className="text-warning">Missing: {i.missing_config.join(", ")}</span> : <span className="text-success">Complete</span>}</Td>
              <Td className="text-xs">v{i.version}{i.update_available && <Badge tone="info" className="ml-1.5">v{i.latest_version} available</Badge>}</Td>
              <Td className="text-xs text-muted">{fmtDay(i.installed_at)}</Td>
              <Td>{install.ok && (
                <div className="flex justify-end gap-1.5">
                  <Button size="sm" onClick={() => { setEditing(i); setConfig(i.config); }}>Configure</Button>
                  {i.update_available && i.workflow_id && <Button size="sm" loading={update.isPending && update.variables === i.id}
                    onClick={() => window.confirm("Replace this workflow's draft with the new template version? The published version keeps running until you review and publish.") && update.mutate(i.id)}>Update</Button>}
                  <Button size="sm" variant="danger" onClick={() => window.confirm(`Remove "${i.template_name}"? Its workflow is deactivated and kept for history.`) && remove.mutate(i.id)}>Remove</Button>
                </div>)}</Td>
            </tr>
          ))}</tbody>
        </Table>
      )}
      <div className="px-4 py-2"><InlineError error={update.error ?? remove.error} /></div>
      <Dialog open={!!editing} onClose={() => setEditing(null)} wide title={`Configure ${editing?.template_name ?? ""}`} description="Changes apply to new runs immediately."
        footer={<><Button onClick={() => setEditing(null)}>Cancel</Button><Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>Save configuration</Button></>}>
        {editing && <TemplateConfig schema={editing.config_schema} value={config} onChange={setConfig} />}
        <InlineError error={save.error} />
      </Dialog>
    </Card>
  );
}

export default function TemplatesPage() {
  const { org } = useSession();
  const [tab, setTab] = useState<"library" | "installed">("library");
  return (
    <>
      <PageHeader title="Templates" description="Ready-made automations. Installing one creates an independent workflow in the selected organization that you can configure, test and adapt." />
      <Tabs tabs={[{ key: "library", label: "Library" }, ...(org ? [{ key: "installed" as const, label: "Installed" }] : [])]} value={org ? tab : "library"} onChange={setTab} />
      {tab === "installed" && org ? <Installed /> : <Library />}
    </>
  );
}
