"use client";

import Link from "next/link";
import { use, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Send, Trash2 } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useLocateOrg, useOrgKey, useSession } from "@/lib/session";
import { fmtNumber, fmtRelative, fmtUsd } from "@/lib/format";
import { IntegrationSelect } from "@/components/schema-form";
import { Badge, Button, Card, CardHeader, Checkbox, Dialog, ErrorState, Field, InlineError, Input, Notice, PageHeader, Select, Spinner, StatusBadge, Table, Tabs, Td, Textarea, Th } from "@/components/ui";

const EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];

function Config({ agent, meta }: { agent: any; meta: any }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const write = can("agents.write");
  const [f, setF] = useState<any>(agent);
  useEffect(() => setF(agent), [agent]);
  const kbs = useQuery({ queryKey: [key, "kbs"], queryFn: () => api<{ items: any[] }>("/knowledge-bases") });
  const save = useMutation({
    mutationFn: () => api(`/agents/${agent.id}`, { method: "PUT", body: {
      name: f.name, description: f.description, agent_type: f.agent_type, system_instructions: f.system_instructions, provider: f.provider, model: f.model,
      fallbacks: (f.fallbacks ?? []).filter((x: any) => x.model), temperature: f.temperature === "" || f.temperature === null ? null : Number(f.temperature),
      max_tokens: Number(f.max_tokens), max_iterations: Number(f.max_iterations), provider_options: f.provider_options ?? {},
      limits: { max_executions_per_day: f.limits?.max_executions_per_day ? Number(f.limits.max_executions_per_day) : null, max_tokens_per_day: f.limits?.max_tokens_per_day ? Number(f.limits.max_tokens_per_day) : null },
      knowledge_base_ids: f.knowledge_base_ids ?? [], status: f.status } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [key, "agent", agent.id] }); qc.invalidateQueries({ queryKey: [key, "agents"] }); } });
  const set = (k: string) => (e: any) => setF({ ...f, [k]: e.target.value });
  const fb = f.fallbacks?.[0] ?? { provider: "anthropic", model: "" };
  const anthropic = f.provider === "anthropic";
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card><CardHeader title="Identity and behaviour" />
        <div className="space-y-3 p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Name" required><Input value={f.name} onChange={set("name")} disabled={!write} /></Field>
            <Field label="Type"><Select value={f.agent_type} onChange={set("agent_type")} disabled={!write}>{meta.agent_types.map((t: any) => <option key={t.key} value={t.key}>{t.label}</option>)}</Select></Field>
          </div>
          <Field label="Description"><Input value={f.description} onChange={set("description")} disabled={!write} /></Field>
          <Field label="System instructions" hint="Who the agent is, what it should and should not do, tone, and when to hand over to a person.">
            <Textarea value={f.system_instructions} onChange={set("system_instructions")} disabled={!write} className="min-h-[220px]" /></Field>
          <Field label="Status"><Select value={f.status} onChange={set("status")} disabled={!write}><option value="active">Active</option><option value="disabled">Disabled</option></Select></Field>
        </div></Card>
      <div className="space-y-5">
        <Card><CardHeader title="Model" description="Credentials come from this organization's AI integration, or the platform key." />
          <div className="space-y-3 p-4">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Provider"><Select value={f.provider} onChange={(e) => setF({ ...f, provider: e.target.value, model: e.target.value === "anthropic" ? meta.default_model : "" })} disabled={!write}>
                {meta.providers.map((p: string) => <option key={p} value={p}>{p.replace("_", " ")}</option>)}</Select></Field>
              <Field label="Model" required hint={anthropic ? undefined : "Enter the exact model id of your provider."}>
                {anthropic ? <Select value={f.model} onChange={set("model")} disabled={!write}>{[...new Set([f.model, ...meta.anthropic_models])].filter(Boolean).map((m: string) => <option key={m} value={m}>{m}</option>)}</Select>
                  : <Input value={f.model} onChange={set("model")} disabled={!write} />}</Field>
            </div>
            <div className="grid grid-cols-3 gap-3">
              <Field label="Max output tokens"><Input type="number" min={256} max={64000} value={f.max_tokens} onChange={set("max_tokens")} disabled={!write} /></Field>
              <Field label="Max tool rounds"><Input type="number" min={1} max={30} value={f.max_iterations} onChange={set("max_iterations")} disabled={!write} /></Field>
              {anthropic ? <Field label="Effort"><Select value={f.provider_options?.effort ?? ""} disabled={!write} onChange={(e) => setF({ ...f, provider_options: e.target.value ? { effort: e.target.value } : {} })}>
                {EFFORTS.map((x) => <option key={x} value={x}>{x || "Model default"}</option>)}</Select></Field>
                : <Field label="Temperature"><Input type="number" min={0} max={2} step={0.1} value={f.temperature ?? ""} onChange={set("temperature")} disabled={!write} placeholder="Provider default" /></Field>}
            </div>
            {anthropic && <p className="text-xs text-muted">Current Claude models do not accept a temperature setting; response depth is controlled with effort.</p>}
            <div className="grid grid-cols-2 gap-3">
              <Field label="Fallback provider" hint="Used when the primary provider is unavailable.">
                <Select value={fb.model ? fb.provider : ""} disabled={!write} onChange={(e) => setF({ ...f, fallbacks: e.target.value ? [{ provider: e.target.value, model: e.target.value === "anthropic" ? "claude-sonnet-5-5" : fb.model }] : [] })}>
                  <option value="">None</option>{meta.providers.map((p: string) => <option key={p} value={p}>{p.replace("_", " ")}</option>)}</Select></Field>
              <Field label="Fallback model"><Input value={fb.model} disabled={!write || !f.fallbacks?.length} onChange={(e) => setF({ ...f, fallbacks: [{ ...fb, model: e.target.value }] })} /></Field>
            </div>
          </div></Card>
        <Card><CardHeader title="Limits and knowledge" />
          <div className="space-y-3 p-4">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Max runs per day" hint="Empty = unlimited"><Input type="number" min={1} value={f.limits?.max_executions_per_day ?? ""} disabled={!write} onChange={(e) => setF({ ...f, limits: { ...f.limits, max_executions_per_day: e.target.value } })} /></Field>
              <Field label="Max tokens per day" hint="Empty = unlimited"><Input type="number" min={1} value={f.limits?.max_tokens_per_day ?? ""} disabled={!write} onChange={(e) => setF({ ...f, limits: { ...f.limits, max_tokens_per_day: e.target.value } })} /></Field>
            </div>
            <div><div className="mb-1 text-xs font-medium text-muted">Knowledge bases</div>
              {(kbs.data?.items ?? []).length === 0 ? <p className="text-xs text-muted">None yet. Create one on the Knowledge tab of AI Agents.</p> : (
                <div className="space-y-1.5">{kbs.data!.items.map((k) => <Checkbox key={k.id} label={k.name} disabled={!write} checked={(f.knowledge_base_ids ?? []).includes(k.id)}
                  onChange={(on) => setF({ ...f, knowledge_base_ids: on ? [...(f.knowledge_base_ids ?? []), k.id] : f.knowledge_base_ids.filter((x: string) => x !== k.id) })} />)}</div>)}
            </div>
          </div></Card>
        <InlineError error={save.error} />
        {write && <Button variant="primary" loading={save.isPending} disabled={!f.name?.trim() || !f.model?.trim()} onClick={() => save.mutate()}>Save agent</Button>}
        {save.isSuccess && !save.isPending && <span className="ml-3 text-xs text-success">Saved</span>}
      </div>
    </div>
  );
}

function Tools({ agent, meta }: { agent: any; meta: any }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const write = can("agents.write");
  const [open, setOpen] = useState(false);
  const [toolKey, setToolKey] = useState("http_request");
  const [name, setName] = useState("");
  const [config, setConfig] = useState<any>({});
  const [approval, setApproval] = useState<boolean | null>(null);
  const def = meta.tools.find((t: any) => t.key === toolKey);
  const integrations = useQuery({ queryKey: [key, "integrations"], queryFn: () => api<{ items: any[] }>("/integrations") });
  const workflows = useQuery({ queryKey: [key, "workflows", "all"], queryFn: () => api<Page<any>>("/workflows", { params: { limit: 200 } }), enabled: open });
  const kbs = useQuery({ queryKey: [key, "kbs"], queryFn: () => api<{ items: any[] }>("/knowledge-bases"), enabled: open });
  const mcp = useQuery({ queryKey: [key, "mcp-tools", config.integration_id], queryFn: () => api<{ items: any[] }>(`/integrations/${config.integration_id}/mcp-tools`), enabled: open && toolKey === "mcp_tool" && !!config.integration_id, retry: false });
  const refresh = () => qc.invalidateQueries({ queryKey: [key, "agent", agent.id] });
  const add = useMutation({ mutationFn: () => api(`/agents/${agent.id}/tools`, { method: "POST", body: { name, tool_key: toolKey, config, requires_approval: approval } }),
    onSuccess: () => { refresh(); setOpen(false); setName(""); setConfig({}); setApproval(null); } });
  const toggle = useMutation({ mutationFn: (t: any) => api(`/agents/${agent.id}/tools/${t.id}`, { method: "PUT", body: { name: t.name, tool_key: t.tool_key, config: t.config, requires_approval: !t.requires_approval } }), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => api(`/agents/${agent.id}/tools/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const pick = (k: string) => { setToolKey(k); setConfig({}); setApproval(null); setName((n) => n || k); };
  const list = (v: string) => v.split(/[\n,]/).map((x) => x.trim()).filter(Boolean);

  return (
    <Card>
      <CardHeader title="Allowed tools" description="The agent can call only these tools, each limited to the resources configured here. It has no other way to act."
        actions={write && <Button size="sm" variant="primary" onClick={() => { setOpen(true); pick("http_request"); setName(""); }}><Plus className="h-3.5 w-3.5" /> Add tool</Button>} />
      {agent.tools.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">This agent has no tools. It can only converse.</p> : (
        <Table><thead><tr><Th>Tool</Th><Th>Scope</Th><Th>Approval</Th><Th /></tr></thead>
          <tbody>{agent.tools.map((t: any) => (
            <tr key={t.id}>
              <Td><span className="font-mono text-xs">{t.name}</span><div className="text-xs text-muted">{meta.tools.find((x: any) => x.key === t.tool_key)?.label ?? t.tool_key}</div></Td>
              <Td className="max-w-xs truncate font-mono text-2xs text-muted">{Object.entries(t.config).filter(([k]) => k !== "input_schema").map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(",") : String(v).slice(0, 24)}`).join("  ") || "-"}</Td>
              <Td>{t.requires_approval ? <Badge tone="warning">Human approval</Badge> : <Badge>Automatic</Badge>}{t.high_impact && !t.requires_approval && <span className="ml-2 text-xs text-warning">high impact</span>}</Td>
              <Td className="text-right">{write && t.name !== "knowledge_search" && (
                <div className="flex justify-end gap-1.5"><Button size="sm" onClick={() => toggle.mutate(t)}>{t.requires_approval ? "Make automatic" : "Require approval"}</Button>
                  <Button size="sm" variant="ghost" aria-label={`Remove ${t.name}`} onClick={() => remove.mutate(t.id)}><Trash2 className="h-3.5 w-3.5" /></Button></div>)}</Td>
            </tr>))}</tbody></Table>
      )}
      <div className="px-4 pb-2"><InlineError error={toggle.error ?? remove.error} /></div>
      <Dialog open={open} onClose={() => setOpen(false)} title="Add tool"
        footer={<><Button onClick={() => setOpen(false)}>Cancel</Button><Button variant="primary" loading={add.isPending} disabled={!/^[a-zA-Z][a-zA-Z0-9_]*$/.test(name)} onClick={() => add.mutate()}>Add tool</Button></>}>
        <Field label="Tool"><Select value={toolKey} onChange={(e) => pick(e.target.value)}>{meta.tools.filter((t: any) => t.key !== "knowledge_search").map((t: any) => <option key={t.key} value={t.key}>{t.label}</option>)}</Select></Field>
        <p className="text-xs text-muted">{def?.description}</p>
        <Field label="Name shown to the model" required hint="Letters, digits and underscores."><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
        {(def?.config_fields ?? []).filter((c: any) => !(toolKey === "mcp_tool" && ["input_schema", "description"].includes(c.name))).map((c: any) => (
          <Field key={c.name} label={c.label} required={c.required} hint={c.help}>
            {c.type === "integration" ? <IntegrationSelect value={config[c.name] ?? ""} onChange={(v) => setConfig({ ...config, [c.name]: v })} integrations={integrations.data?.items ?? []}
              providers={toolKey === "mcp_tool" ? ["mcp_server"] : toolKey === "send_email" ? ["gmail", "outlook", "microsoft_365", "smtp"] : toolKey === "send_message" ? ["whatsapp", "telegram", "slack", "discord"]
                : toolKey === "create_crm_lead" ? ["hubspot", "pipedrive", "salesforce", "mailchimp"] : toolKey === "create_calendar_event" ? ["google_calendar", "outlook", "microsoft_365"] : undefined} />
              : c.type === "workflow" ? <Select value={config[c.name] ?? ""} onChange={(e) => setConfig({ ...config, [c.name]: e.target.value })}><option value="">Select a workflow...</option>{(workflows.data?.items ?? []).map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}</Select>
              : c.type === "knowledge_bases" ? <div className="space-y-1.5">{(kbs.data?.items ?? []).map((k) => <Checkbox key={k.id} label={k.name} checked={(config[c.name] ?? []).includes(k.id)} onChange={(on) => setConfig({ ...config, [c.name]: on ? [...(config[c.name] ?? []), k.id] : config[c.name].filter((x: string) => x !== k.id) })} />)}</div>
              : c.type === "string_list" ? <Textarea className="min-h-[60px] font-mono text-xs" placeholder="One per line" value={(config[c.name] ?? []).join("\n")} onChange={(e) => setConfig({ ...config, [c.name]: list(e.target.value) })} />
              : toolKey === "mcp_tool" && c.name === "tool_name" ? (
                mcp.isLoading ? <Spinner label="Discovering tools" /> : mcp.error ? <InlineError error={mcp.error} /> : !config.integration_id ? <p className="text-xs text-muted">Choose the MCP server first.</p> : (
                  <Select value={config.tool_name ?? ""} onChange={(e) => { const t = mcp.data?.items.find((x) => x.name === e.target.value); setConfig({ ...config, tool_name: e.target.value, input_schema: t?.input_schema, description: t?.description }); setName(e.target.value.replace(/[^a-zA-Z0-9_]/g, "_")); }}>
                    <option value="">Select a remote tool...</option>{(mcp.data?.items ?? []).map((t) => <option key={t.name} value={t.name}>{t.name}</option>)}</Select>))
              : <Input value={config[c.name] ?? ""} onChange={(e) => setConfig({ ...config, [c.name]: e.target.value })} />}
          </Field>
        ))}
        <Checkbox label="Require human approval before each use" checked={approval ?? Boolean(def?.high_impact)} onChange={setApproval} />
        {def?.high_impact && approval === false && <Notice tone="warning">This tool changes external systems. Without approval the agent can use it on its own.</Notice>}
        <InlineError error={add.error} />
      </Dialog>
    </Card>
  );
}

function Chat({ agent }: { agent: any }) {
  const key = useOrgKey();
  const [conversation, setConversation] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [last, setLast] = useState<any>(null);
  const end = useRef<HTMLDivElement>(null);
  const messages = useQuery({ queryKey: [key, "messages", conversation], queryFn: () => api<{ items: any[] }>(`/conversations/${conversation}/messages`), enabled: !!conversation });
  const exec = useQuery({ queryKey: [key, "agent-exec", last?.id], queryFn: () => api(`/agent-executions/${last.id}`), enabled: last?.status === "WAITING_APPROVAL",
    refetchInterval: (q) => ((q.state.data as any)?.status === "WAITING_APPROVAL" || !q.state.data ? 3000 : false) });
  const send = useMutation({
    mutationFn: (message: string) => api(`/agents/${agent.id}/chat`, { method: "POST", body: { message, conversation_id: conversation } }),
    onSuccess: (r: any) => { setLast(r); setConversation(r.conversation_id); setText(""); messages.refetch(); } });
  const current = exec.data ?? last;
  useEffect(() => { if (exec.data && exec.data.status !== "WAITING_APPROVAL") messages.refetch(); }, [exec.data?.status]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [messages.data, send.isPending]);
  return (
    <Card className="flex h-[620px] flex-col">
      <CardHeader title="Test conversation" description="Runs the saved agent with its real tools." actions={conversation && <Button size="sm" onClick={() => { setConversation(null); setLast(null); }}>New conversation</Button>} />
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {!conversation && !send.isPending && <p className="pt-16 text-center text-sm text-muted">Send a message to try the agent.</p>}
        {(messages.data?.items ?? []).map((m) => m.role === "tool" ? (
          <div key={m.id} className="mx-auto max-w-[90%] space-y-1">{m.tool_results.map((r: any, i: number) => (
            <details key={i} className="rounded-md border bg-bg px-3 py-1.5 text-xs"><summary className="cursor-pointer text-muted">Tool result: <span className="font-mono">{r.name}</span> {r.is_error && <span className="text-danger">(error)</span>}</summary>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-2xs">{r.content}</pre></details>))}</div>
        ) : (
          <div key={m.id} className={m.role === "user" ? "flex justify-end" : "flex justify-start"}>
            <div className={`max-w-[80%] rounded-lg px-3 py-2 text-sm ${m.role === "user" ? "bg-primary text-primary-fg" : "border bg-raised"}`}>
              {m.text && <div className="whitespace-pre-wrap">{m.text}</div>}
              {m.tool_calls.map((c: any) => <div key={c.id} className="mt-1 font-mono text-2xs opacity-80">calls {c.name}({JSON.stringify(c.input).slice(0, 120)})</div>)}
            </div>
          </div>
        ))}
        {send.isPending && <div className="text-xs text-muted">The agent is working...</div>}
        {current?.status === "WAITING_APPROVAL" && <Notice tone="warning">The agent wants to use a tool that needs approval. <Link href="/executions" className="underline">Review it under Executions - Approvals</Link>; the conversation continues automatically.</Notice>}
        {current?.status === "FAILED" && <Notice tone="warning">{current.error}</Notice>}
        <div ref={end} />
      </div>
      {current && <div className="border-t px-4 py-1.5 text-2xs uppercase tracking-wider text-muted">Last run: <StatusBadge status={current.status} /> {current.model} - {fmtNumber(current.input_tokens + current.output_tokens)} tokens - {current.cost_usd === null ? "cost unknown (model unpriced)" : fmtUsd(current.cost_usd)}</div>}
      <form className="flex gap-2 border-t p-3" onSubmit={(e) => { e.preventDefault(); if (text.trim() && !send.isPending) send.mutate(text.trim()); }}>
        <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Message the agent..." aria-label="Message" disabled={agent.status !== "active"} />
        <Button type="submit" variant="primary" loading={send.isPending} disabled={!text.trim() || agent.status !== "active"} aria-label="Send"><Send className="h-4 w-4" /></Button>
      </form>
      <div className="px-3 pb-2"><InlineError error={send.error} /></div>
    </Card>
  );
}

function Runs({ agent }: { agent: any }) {
  const key = useOrgKey();
  const runs = useQuery({ queryKey: [key, "agent-runs", agent.id], queryFn: () => api<Page<any>>(`/agents/${agent.id}/executions`, { params: { limit: 50 } }) });
  if (runs.isLoading) return <Spinner />;
  if (runs.error) return <ErrorState error={runs.error} />;
  return (
    <Card>{runs.data!.items.length === 0 ? <p className="px-4 py-10 text-center text-sm text-muted">This agent has not run yet.</p> : (
      <Table><thead><tr><Th>When</Th><Th>Status</Th><Th>Model</Th><Th className="text-right">Rounds</Th><Th className="text-right">Tokens</Th><Th className="text-right">Est. cost</Th><Th>Tools used</Th></tr></thead>
        <tbody>{runs.data!.items.map((r) => (
          <tr key={r.id}><Td className="text-xs text-muted">{fmtRelative(r.created_at)}</Td><Td><StatusBadge status={r.status} />{r.error && <div className="max-w-xs truncate text-xs text-danger">{r.error}</div>}</Td>
            <Td className="font-mono text-xs">{r.model}</Td><Td className="tabular text-right">{r.iterations}</Td><Td className="tabular text-right">{fmtNumber(r.input_tokens + r.output_tokens)}</Td>
            <Td className="tabular text-right">{r.cost_usd === null ? "-" : fmtUsd(r.cost_usd)}</Td>
            <Td className="font-mono text-2xs text-muted">{r.trace.filter((t: any) => t.type === "tool").map((t: any) => t.name + (t.is_error ? "!" : "")).join(", ") || "-"}</Td></tr>
        ))}</tbody></Table>)}</Card>
  );
}

export default function AgentPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const key = useOrgKey();
  const router = useRouter();
  const qc = useQueryClient();
  const { org, can } = useSession();
  const [tab, setTab] = useState<"config" | "tools" | "chat" | "runs">("config");
  const locating = useLocateOrg("agent", id);
  const agent = useQuery({ queryKey: [key, "agent", id], queryFn: () => api(`/agents/${id}`), enabled: !!org });
  const meta = useQuery({ queryKey: [key, "agents-meta"], queryFn: () => api("/agents/meta"), enabled: !!org, staleTime: 600_000 });
  const remove = useMutation({ mutationFn: () => api(`/agents/${id}`, { method: "DELETE" }), onSuccess: () => { qc.invalidateQueries({ queryKey: [key, "agents"] }); router.push("/agents"); } });
  if (locating) return <Spinner />;
  if (!org) return <Notice tone="warning">Select the organization this agent belongs to.</Notice>;
  if (agent.isLoading || meta.isLoading) return <Spinner />;
  if (agent.error || meta.error) return <ErrorState error={agent.error ?? meta.error} onRetry={() => agent.refetch()} />;
  const a = agent.data;
  return (
    <>
      <div className="mb-2 text-xs text-muted"><Link href="/agents" className="hover:text-fg">AI Agents</Link> / {a.name}</div>
      <PageHeader title={a.name} description={<span className="flex items-center gap-2"><StatusBadge status={a.status} /><span className="font-mono text-xs">{a.provider} / {a.model}</span></span>}
        actions={can("agents.write") && <Button variant="danger" loading={remove.isPending} onClick={() => window.confirm(`Delete agent "${a.name}"?`) && remove.mutate()}>Delete</Button>} />
      <InlineError error={remove.error} />
      <Tabs tabs={[{ key: "config", label: "Configuration" }, { key: "tools", label: "Tools", count: a.tools.length }, ...(can("agents.run") ? [{ key: "chat" as const, label: "Test" }] : []), { key: "runs", label: "Runs" }]} value={tab} onChange={setTab} />
      {tab === "config" ? <Config agent={a} meta={meta.data} /> : tab === "tools" ? <Tools agent={a} meta={meta.data} /> : tab === "chat" ? <Chat agent={a} /> : <Runs agent={a} />}
    </>
  );
}
