"use client";

import Link from "next/link";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Bot, Plus, Trash2 } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDay, fmtNumber } from "@/lib/format";
import { Button, Card, CardHeader, Dialog, EmptyState, ErrorState, Field, InlineError, Input, NeedOrg, PageHeader, Select, Spinner, StatusBadge, Table, Tabs, Td, Textarea, Th } from "@/components/ui";

function AgentsTab() {
  const key = useOrgKey();
  const router = useRouter();
  const qc = useQueryClient();
  const { can } = useSession();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ name: "", agent_type: "customer_support", description: "" });
  const meta = useQuery({ queryKey: [key, "agents-meta"], queryFn: () => api("/agents/meta"), staleTime: 600_000 });
  const list = useQuery({ queryKey: [key, "agents"], queryFn: () => api<Page<any>>("/agents", { params: { limit: 200 } }) });
  const create = useMutation({
    mutationFn: () => api("/agents", { method: "POST", body: { ...form, provider: "anthropic", model: meta.data?.default_model ?? "claude-opus-5-5" } }),
    onSuccess: (a: any) => { qc.invalidateQueries({ queryKey: [key, "agents"] }); router.push(`/agents/${a.id}`); } });
  const write = can("agents.write");
  return (
    <Card>
      <CardHeader title="Agents" description="Each agent has its own instructions, model, knowledge and an explicit list of allowed tools."
        actions={write && <Button size="sm" variant="primary" onClick={() => setOpen(true)}><Plus className="h-3.5 w-3.5" /> New agent</Button>} />
      {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
        <EmptyState icon={<Bot className="h-5 w-5" />} title="No AI agents yet" description="Create an agent, give it instructions and tools, then chat with it or call it from a workflow."
          action={write && <Button variant="primary" onClick={() => setOpen(true)}>New agent</Button>} />
      ) : (
        <Table>
          <thead><tr><Th>Agent</Th><Th>Type</Th><Th>Model</Th><Th className="text-right">Tools</Th><Th>Status</Th></tr></thead>
          <tbody>{list.data!.items.map((a) => (
            <tr key={a.id} className="hover:bg-raised/50">
              <Td><Link href={`/agents/${a.id}`} className="font-medium hover:text-primary">{a.name}</Link>{a.description && <div className="max-w-md truncate text-xs text-muted">{a.description}</div>}</Td>
              <Td className="text-xs text-muted">{(meta.data?.agent_types ?? []).find((t: any) => t.key === a.agent_type)?.label ?? a.agent_type}</Td>
              <Td className="font-mono text-xs">{a.provider} / {a.model}</Td>
              <Td className="tabular text-right">{a.tool_count}</Td>
              <Td><StatusBadge status={a.status} /></Td>
            </tr>
          ))}</tbody>
        </Table>
      )}
      <Dialog open={open} onClose={() => setOpen(false)} title="New agent" description="You can refine instructions, model and tools on the next screen."
        footer={<><Button onClick={() => setOpen(false)}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!form.name.trim()} onClick={() => create.mutate()}>Create agent</Button></>}>
        <Field label="Name" required><Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} autoFocus /></Field>
        <Field label="Type"><Select value={form.agent_type} onChange={(e) => setForm({ ...form, agent_type: e.target.value })}>{(meta.data?.agent_types ?? []).map((t: any) => <option key={t.key} value={t.key}>{t.label}</option>)}</Select></Field>
        <Field label="Description"><Textarea value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></Field>
        <InlineError error={create.error} />
      </Dialog>
    </Card>
  );
}

function KnowledgeTab() {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const write = can("knowledge.write");
  const [name, setName] = useState("");
  const [active, setActive] = useState<any | null>(null);
  const [doc, setDoc] = useState({ title: "", content: "", source: "" });
  const [query, setQuery] = useState("");
  const kbs = useQuery({ queryKey: [key, "kbs"], queryFn: () => api<{ items: any[] }>("/knowledge-bases") });
  const docs = useQuery({ queryKey: [key, "kb-docs", active?.id], queryFn: () => api<Page<any>>(`/knowledge-bases/${active.id}/documents`, { params: { limit: 100 } }), enabled: !!active });
  const refresh = () => { qc.invalidateQueries({ queryKey: [key, "kbs"] }); qc.invalidateQueries({ queryKey: [key, "kb-docs"] }); };
  const create = useMutation({ mutationFn: () => api("/knowledge-bases", { method: "POST", body: { name } }), onSuccess: () => { setName(""); refresh(); } });
  const removeKb = useMutation({ mutationFn: (id: string) => api(`/knowledge-bases/${id}`, { method: "DELETE" }), onSuccess: () => { setActive(null); refresh(); } });
  const addDoc = useMutation({ mutationFn: () => api(`/knowledge-bases/${active.id}/documents`, { method: "POST", body: doc }), onSuccess: () => { setDoc({ title: "", content: "", source: "" }); refresh(); } });
  const removeDoc = useMutation({ mutationFn: (id: string) => api(`/knowledge-bases/${active.id}/documents/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const search = useMutation({ mutationFn: () => api<{ results: any[] }>(`/knowledge-bases/${active.id}/search`, { method: "POST", body: { query } }) });

  return (
    <div className="grid gap-5 lg:grid-cols-[320px_1fr]">
      <Card>
        <CardHeader title="Knowledge bases" description="Documents agents can search. Retrieval is keyword based (BM25)." />
        {kbs.isLoading ? <Spinner /> : kbs.error ? <div className="p-4"><ErrorState error={kbs.error} /></div> : (
          <ul className="divide-y divide-line/60">
            {kbs.data!.items.length === 0 && <li className="px-4 py-6 text-center text-xs text-muted">No knowledge bases yet.</li>}
            {kbs.data!.items.map((k) => (
              <li key={k.id}><button type="button" onClick={() => { setActive(k); search.reset(); }} className={`flex w-full items-center justify-between px-4 py-2.5 text-left text-sm hover:bg-raised/50 ${active?.id === k.id ? "bg-raised" : ""}`}>
                <span className="font-medium">{k.name}</span><span className="tabular text-xs text-muted">{fmtNumber(k.documents)} docs</span></button></li>
            ))}
          </ul>
        )}
        {write && <div className="flex gap-2 border-t p-3"><Input value={name} onChange={(e) => setName(e.target.value)} placeholder="New knowledge base name" aria-label="New knowledge base name" />
          <Button variant="primary" loading={create.isPending} disabled={!name.trim()} onClick={() => create.mutate()}>Add</Button></div>}
        <div className="px-3 pb-3"><InlineError error={create.error} /></div>
      </Card>
      {!active ? <Card><EmptyState icon={<BookOpen className="h-5 w-5" />} title="Select a knowledge base" description="Add documents such as FAQs, policies, menus or price lists, then attach the knowledge base to an agent." /></Card> : (
        <div className="space-y-5">
          <Card>
            <CardHeader title={active.name} actions={write && <Button size="sm" variant="danger" onClick={() => window.confirm(`Delete "${active.name}" and all of its documents?`) && removeKb.mutate(active.id)}>Delete</Button>} />
            {docs.isLoading ? <Spinner /> : docs.error ? <div className="p-4"><ErrorState error={docs.error} /></div> : docs.data!.items.length === 0 ? <p className="px-4 py-6 text-center text-sm text-muted">No documents yet.</p> : (
              <Table><thead><tr><Th>Document</Th><Th className="text-right">Characters</Th><Th className="text-right">Added</Th><Th /></tr></thead>
                <tbody>{docs.data!.items.map((d) => (
                  <tr key={d.id}><Td><span className="font-medium">{d.title}</span>{d.source && <div className="text-xs text-muted">{d.source}</div>}</Td><Td className="tabular text-right">{fmtNumber(d.characters)}</Td>
                    <Td className="text-right text-xs text-muted">{fmtDay(d.created_at)}</Td>
                    <Td className="text-right">{write && <Button size="sm" variant="ghost" aria-label={`Delete ${d.title}`} onClick={() => removeDoc.mutate(d.id)}><Trash2 className="h-3.5 w-3.5" /></Button>}</Td></tr>
                ))}</tbody></Table>
            )}
            <div className="px-4 pb-2"><InlineError error={removeKb.error ?? removeDoc.error} /></div>
          </Card>
          {write && (
            <Card><CardHeader title="Add document" description="Paste text. It is split into passages and indexed immediately." />
              <div className="space-y-3 p-4">
                <div className="grid grid-cols-2 gap-3"><Field label="Title" required><Input value={doc.title} onChange={(e) => setDoc({ ...doc, title: e.target.value })} /></Field>
                  <Field label="Source (optional)"><Input value={doc.source} onChange={(e) => setDoc({ ...doc, source: e.target.value })} placeholder="URL or file name" /></Field></div>
                <Field label="Content" required><Textarea value={doc.content} onChange={(e) => setDoc({ ...doc, content: e.target.value })} className="min-h-[160px]" /></Field>
                <InlineError error={addDoc.error} />
                <Button variant="primary" loading={addDoc.isPending} disabled={!doc.title.trim() || !doc.content.trim()} onClick={() => addDoc.mutate()}>Add document</Button>
              </div></Card>
          )}
          <Card><CardHeader title="Test retrieval" />
            <div className="space-y-3 p-4">
              <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); if (query.trim()) search.mutate(); }}>
                <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Ask something the documents answer..." aria-label="Search query" />
                <Button type="submit" loading={search.isPending} disabled={!query.trim()}>Search</Button></form>
              <InlineError error={search.error} />
              {search.data && (search.data.results.length === 0 ? <p className="text-sm text-muted">No passages matched.</p> : (
                <ul className="space-y-2">{search.data.results.map((r, i) => <li key={i} className="rounded-md border bg-bg p-3 text-sm"><div className="mb-1 text-2xs uppercase tracking-wider text-muted">{r.title} - score {r.score}</div>{r.content}</li>)}</ul>
              ))}
            </div></Card>
        </div>
      )}
    </div>
  );
}

export default function AgentsPage() {
  const { org } = useSession();
  const [tab, setTab] = useState<"agents" | "knowledge">("agents");
  if (!org) return <><PageHeader title="AI Agents" /><NeedOrg what="Agents" /></>;
  return (
    <>
      <PageHeader title="AI Agents" description="Agents act only through the tools you explicitly allow. High-impact actions can require human approval." />
      <Tabs tabs={[{ key: "agents", label: "Agents" }, { key: "knowledge", label: "Knowledge" }]} value={tab} onChange={setTab} />
      {tab === "agents" ? <AgentsTab /> : <KnowledgeTab />}
    </>
  );
}
