"use client";

import Link from "next/link";
import { createContext, memo, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Background, Controls, Handle, MiniMap, Position, ReactFlow, ReactFlowProvider, addEdge, useEdgesState, useNodesState, useReactFlow,
  type Connection, type Edge, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import clsx from "clsx";
import { AlertTriangle, ArrowLeft, Check, Copy, FlaskConical, History, Play, Plus, Rocket, Save, Search, Trash2, X } from "lucide-react";
import { api, errorMessage } from "@/lib/api";
import { useLocateOrg, useOrgKey, useSession } from "@/lib/session";
import { fmtDate, fmtDuration, fmtRelative } from "@/lib/format";
import { SchemaForm } from "@/components/schema-form";
import { Badge, Button, Checkbox, Code, Dialog, Field, InlineError, Input, Notice, Spinner, StatusBadge, Tabs, Textarea } from "@/components/ui";
import { CATEGORY, handlesFor, makeEdge, newNodeId, toDefinition, toFlow, type Definition, type NodeSpec, type WaeNode } from "./model";

type Issue = { level: "error" | "warning"; node_id: string | null; message: string };
type BuilderCtx = { specs: Record<string, NodeSpec>; issues: Issue[]; runStatus: Record<string, string> };
const Ctx = createContext<BuilderCtx>({ specs: {}, issues: [], runStatus: {} });

const NODE_W = 224; // w-56
const NODE_H = 88;
const RUN_RING: Record<string, string> = { COMPLETED: "ring-success/70", FAILED: "ring-danger/80", RUNNING: "ring-info/70", WAITING: "ring-warning/70", SKIPPED: "ring-line" };

const FlowNode = memo(function FlowNode({ id, data, selected }: NodeProps<WaeNode>) {
  const { specs, issues, runStatus } = useContext(Ctx);
  const spec = specs[data.type];
  const handles = handlesFor(spec, data.config);
  const cat = CATEGORY[spec?.category ?? "action"];
  const problems = issues.filter((i) => i.node_id === id);
  const hasError = problems.some((p) => p.level === "error");
  const status = runStatus[id];
  return (
    <div
      className={clsx("w-56 rounded-lg border bg-surface text-left shadow-sm", selected ? "border-primary" : hasError ? "border-danger/60" : "border-line",
        status && "ring-2", status && RUN_RING[status])}
      style={{ minHeight: 44 + Math.max(0, handles.length - 1) * 22 }}
    >
      {spec?.category !== "trigger" && <Handle type="target" position={Position.Left} className="!h-2.5 !w-2.5" />}
      <div className="flex items-center gap-2 rounded-t-lg border-b px-2.5 py-1.5" style={{ background: `rgb(${cat.color} / 0.08)` }}>
        <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: `rgb(${cat.color})` }} aria-hidden />
        <span className="truncate text-2xs font-medium uppercase tracking-wider text-muted">{spec?.label ?? data.type}</span>
        {problems.length > 0 && <AlertTriangle className={clsx("ml-auto h-3.5 w-3.5 shrink-0", hasError ? "text-danger" : "text-warning")} aria-label={problems[0].message} />}
      </div>
      <div className="px-2.5 py-2">
        <div className="truncate text-sm font-medium">{data.name || spec?.label || id}</div>
        <div className="truncate font-mono text-2xs text-muted">{id}</div>
      </div>
      {handles.map((h, i) => (
        <div key={h}>
          <Handle type="source" id={h} position={Position.Right} className="!h-2.5 !w-2.5" style={{ top: 30 + i * 22, background: h === "error" ? "rgb(248 113 113)" : undefined }} />
          {handles.length > 1 && <span className="pointer-events-none absolute right-3 text-2xs text-muted" style={{ top: 22 + i * 22 }}>{h}</span>}
        </div>
      ))}
    </div>
  );
});

const nodeTypes = { wae: FlowNode };

function Palette({ specs, onAdd, open, onClose }: { specs: NodeSpec[]; onAdd: (s: NodeSpec) => void; open: boolean; onClose: () => void }) {
  const [q, setQ] = useState("");
  const shown = specs.filter((s) => !q || `${s.label} ${s.description}`.toLowerCase().includes(q.toLowerCase()));
  return (
    // A column on large screens; on phones and tablets a drawer opened with "Add step".
    <aside aria-label="Steps" className={clsx("flex w-60 shrink-0 flex-col border-r bg-surface",
      open ? "max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:z-40 max-lg:w-72 max-lg:max-w-[85vw] max-lg:shadow-2xl" : "max-lg:hidden")}>
      <div className="flex items-center justify-between border-b px-3 py-2 lg:hidden">
        <span className="text-sm font-medium">Add a step</span>
        <button type="button" onClick={onClose} aria-label="Close steps" className="rounded-md p-1.5 text-muted hover:bg-raised hover:text-fg"><X className="h-4 w-4" /></button>
      </div>
      <div className="border-b p-2.5">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-muted" aria-hidden />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search steps..." className="h-8 pl-8 text-xs" aria-label="Search steps" />
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-2">
        {Object.entries(CATEGORY).map(([key, cat]) => {
          const items = shown.filter((s) => s.category === key);
          if (!items.length) return null;
          return (
            <div key={key} className="mb-3">
              <div className="mb-1 flex items-center gap-1.5 px-1.5 text-2xs font-medium uppercase tracking-wider text-muted">
                <span className="h-1.5 w-1.5 rounded-full" style={{ background: `rgb(${cat.color})` }} aria-hidden />{cat.label}
              </div>
              {items.map((s) => (
                <button key={s.type} type="button" title={s.description} onClick={() => onAdd(s)}
                  className="block w-full rounded-md px-2 py-1.5 text-left text-xs text-fg/90 hover:bg-raised">
                  {s.label}
                </button>
              ))}
            </div>
          );
        })}
        {shown.length === 0 && <p className="px-2 py-6 text-center text-xs text-muted">No steps match.</p>}
      </div>
    </aside>
  );
}

function TriggerInfo({ endpoint, schedule, workflowId, onChanged }: { endpoint?: any; schedule?: any; workflowId: string; onChanged: () => void }) {
  const [secret, setSecret] = useState<string | null>(null);
  const [provided, setProvided] = useState("");
  const [copied, setCopied] = useState(false);
  const rotate = useMutation({ mutationFn: () => api(`/webhooks/${endpoint.id}/rotate-secret`, { method: "POST" }), onSuccess: (r: any) => { setSecret(r.secret); onChanged(); } });
  const store = useMutation({ mutationFn: () => api(`/webhooks/${endpoint.id}/secret`, { method: "PUT", body: { secret: provided } }), onSuccess: () => { setProvided(""); onChanged(); } });
  const verify = useMutation({ mutationFn: () => api<{ verify_token: string }>(`/webhooks/${endpoint.id}/verify-token`) });
  if (schedule) {
    return <Notice>Schedule <span className="font-mono">{schedule.cron}</span> ({schedule.timezone}). {schedule.is_active ? `Next run ${fmtRelative(schedule.next_run_at)}.` : "Runs once the workflow is published and active."}</Notice>;
  }
  if (!endpoint) return <Notice>Save the draft to generate this trigger's URL.</Notice>;
  const scheme = endpoint.signature_scheme;
  const HELP: Record<string, string> = {
    hmac_sha256: "Sign each request: header X-WAE-Timestamp (unix seconds) and X-WAE-Signature = 'sha256=' + HMAC-SHA256(secret, timestamp + '.' + body).",
    shared_secret: "Send the secret in the X-WAE-Secret header.",
    telegram: "Register this URL with setWebhook and pass the secret as secret_token.",
    stripe: "Add this URL as a Stripe webhook endpoint and store its signing secret (whsec_...) below.",
    meta: "Subscribe this URL in the Meta developer console with the verify token, and store the app secret below.",
    slack: "Use this URL as the Events API request URL and store the Slack signing secret below.",
    none: "This endpoint accepts unauthenticated requests (public form). It is rate limited.",
  };
  const ownSecret = ["hmac_sha256", "shared_secret", "telegram"].includes(scheme);
  return (
    <div className="space-y-2 rounded-md border bg-bg p-2.5">
      <div className="text-2xs font-medium uppercase tracking-wider text-muted">Trigger URL</div>
      <div className="flex gap-1.5">
        <Input readOnly value={endpoint.url} className="h-8 font-mono text-2xs" aria-label="Trigger URL" onFocus={(e) => e.target.select()} />
        <Button size="sm" aria-label="Copy URL" onClick={async () => { try { await navigator.clipboard.writeText(endpoint.url); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* clipboard blocked: the field is selectable */ } }}>
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        </Button>
      </div>
      <p className="text-xs leading-relaxed text-muted">{HELP[scheme]}</p>
      {scheme !== "none" && !endpoint.has_secret && <Notice tone="warning">No signing secret is stored yet - requests are rejected until one is set.</Notice>}
      {ownSecret && (
        <>
          <Button size="sm" loading={rotate.isPending} onClick={() => (endpoint.has_secret ? window.confirm("Generate a new secret? The current one stops working immediately.") : true) && rotate.mutate()}>
            {endpoint.has_secret ? "Rotate secret" : "Generate secret"}
          </Button>
          {secret && <Notice tone="success">New secret (shown once): <span className="break-all font-mono">{secret}</span></Notice>}
        </>
      )}
      {!ownSecret && scheme !== "none" && (
        <div className="flex gap-1.5">
          <Input type="password" autoComplete="off" value={provided} onChange={(e) => setProvided(e.target.value)} placeholder="Provider signing secret" className="h-8 text-xs" aria-label="Provider signing secret" />
          <Button size="sm" loading={store.isPending} disabled={provided.length < 8} onClick={() => store.mutate()}>Store</Button>
        </div>
      )}
      {scheme === "meta" && (
        <div><Button size="sm" loading={verify.isPending} onClick={() => verify.mutate()}>Show verify token</Button>
          {verify.data && <p className="mt-1 break-all font-mono text-xs">{verify.data.verify_token}</p>}</div>
      )}
      <InlineError error={rotate.error ?? store.error ?? verify.error} />
    </div>
  );
}

function MailboxInfo({ mailbox }: { mailbox?: any }) {
  if (!mailbox || !mailbox.is_active) {
    return <Notice>Checks the mailbox every few minutes once the workflow is published and active. Only mail that arrives after activation starts it.</Notice>;
  }
  if (mailbox.last_error) {
    return <Notice tone="warning">The mailbox could not be read: {mailbox.last_error}. Next attempt {fmtRelative(mailbox.next_poll_at)}.</Notice>;
  }
  return (
    <Notice tone="success">
      Watching <span className="font-mono">{mailbox.folder}</span> every {mailbox.poll_minutes} min.{" "}
      {mailbox.watching ? `Last checked ${fmtRelative(mailbox.last_polled_at)}.` : "The first check notes where new mail begins."}
    </Notice>
  );
}

function NodePanel({ node, spec, issues, triggers, workflowId, onChange, onDelete, onDuplicate, onTriggersChanged }: {
  node: WaeNode; spec: NodeSpec | undefined; issues: Issue[]; triggers: any; workflowId: string;
  onChange: (patch: Partial<WaeNode["data"]>) => void; onDelete: () => void; onDuplicate: () => void; onTriggersChanged: () => void;
}) {
  const retry = node.data.settings?.retry ?? {};
  const setRetry = (patch: any) => onChange({ settings: { ...node.data.settings, retry: { ...retry, ...patch } } });
  const outputs = Object.keys(spec?.output_schema?.properties ?? {});
  const endpoint = triggers?.endpoints?.find((e: any) => e.node_id === node.id);
  const schedule = triggers?.schedules?.find((s: any) => s.node_id === node.id);
  const mailbox = triggers?.mailboxes?.find((m: any) => m.node_id === node.id);
  const isEndpointTrigger = spec?.category === "trigger" && !["trigger.manual", "trigger.app_event", "trigger.mailbox"].includes(node.data.type);
  return (
    <div className="space-y-4 p-4">
      <div>
        <div className="flex items-center gap-2">
          <Badge tone="primary">{spec?.label ?? node.data.type}</Badge>
          {spec?.high_impact && <Badge tone="warning">High impact</Badge>}
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted">{spec?.description}</p>
      </div>
      {issues.map((i, k) => <Notice key={k} tone={i.level === "error" ? "warning" : "info"}>{i.message}</Notice>)}
      <Field label="Step name"><Input value={node.data.name} onChange={(e) => onChange({ name: e.target.value })} maxLength={200} /></Field>
      {node.data.type === "trigger.mailbox" && <MailboxInfo mailbox={mailbox} />}
      {isEndpointTrigger && <TriggerInfo endpoint={endpoint} schedule={node.data.type === "trigger.schedule" ? schedule ?? { cron: node.data.config.cron, timezone: node.data.config.timezone ?? "UTC", is_active: false } : undefined} workflowId={workflowId} onChanged={onTriggersChanged} />}
      {spec ? <SchemaForm schema={spec.config_schema} value={node.data.config} onChange={(config) => onChange({ config })} providers={spec.providers} />
        : <Notice tone="warning">This step type is not available in the node registry. Remove it to publish the workflow.</Notice>}
      {spec && spec.category !== "trigger" && spec.category !== "logic" && (
        <div className="space-y-2.5 rounded-md border bg-bg p-2.5">
          <div className="text-2xs font-medium uppercase tracking-wider text-muted">Failure handling</div>
          <Field label="Attempts on transient errors" hint="1 = no automatic retry. Retries use exponential back-off with jitter.">
            <Input type="number" min={1} max={10} value={retry.max_attempts ?? 1} onChange={(e) => setRetry({ max_attempts: Math.max(1, Math.min(10, Number(e.target.value) || 1)) })} />
          </Field>
          {!spec.idempotent && (retry.max_attempts ?? 1) > 1 && (
            <Checkbox label="This step is safe to repeat (allow retrying it)" checked={Boolean(retry.allow_non_idempotent)} onChange={(v) => setRetry({ allow_non_idempotent: v })} />
          )}
          {!spec.idempotent && <p className="text-xs text-muted">This step changes an external system, so it is never repeated automatically unless you allow it.</p>}
          <p className="text-xs text-muted">Connect the red <span className="font-mono">error</span> output to handle failures instead of failing the run.</p>
        </div>
      )}
      <div className="rounded-md border bg-bg p-2.5 text-xs text-muted">
        Use this step's result in later steps as <span className="font-mono text-fg">{`{{ nodes.${node.id}.${outputs[0] ?? "field"} }}`}</span>
        {outputs.length > 0 && <div className="mt-1.5 flex flex-wrap gap-1">{outputs.map((o) => <span key={o} className="rounded-sm border px-1 font-mono text-2xs">{o}</span>)}</div>}
      </div>
      <div className="flex gap-2">
        <Button size="sm" onClick={onDuplicate}><Copy className="h-3.5 w-3.5" /> Duplicate</Button>
        <Button size="sm" variant="danger" onClick={onDelete}><Trash2 className="h-3.5 w-3.5" /> Delete</Button>
      </div>
    </div>
  );
}

function WorkflowPanel({ workflow, issues, dirty, onSelect, onReload }: { workflow: any; issues: Issue[]; dirty: boolean; onSelect: (id: string) => void; onReload: () => void }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"checks" | "variables" | "versions">("checks");
  const [vars, setVars] = useState(JSON.stringify(workflow.variables ?? {}, null, 2));
  const [timeout, setTimeoutS] = useState(String(workflow.settings?.timeout_seconds ?? ""));
  const versions = useQuery({ queryKey: [key, "versions", workflow.id], queryFn: () => api<{ items: any[] }>(`/workflows/${workflow.id}/versions`), enabled: tab === "versions" });
  let parsed: Record<string, unknown> | null = null;
  try { const v = JSON.parse(vars || "{}"); parsed = v && typeof v === "object" && !Array.isArray(v) ? v : null; } catch { parsed = null; }
  const save = useMutation({
    mutationFn: () => api(`/workflows/${workflow.id}`, { method: "PATCH", body: { variables: parsed, settings: { ...(workflow.settings ?? {}), timeout_seconds: timeout ? Number(timeout) : undefined } } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [key, "workflow", workflow.id] }) });
  const restore = useMutation({ mutationFn: (id: string) => api(`/workflows/${workflow.id}/versions/${id}/restore`, { method: "POST" }), onSuccess: onReload });
  return (
    <div className="p-4">
      <Tabs tabs={[{ key: "checks", label: "Checks", count: issues.length }, { key: "variables", label: "Variables" }, { key: "versions", label: "Versions" }]} value={tab} onChange={setTab} />
      {tab === "checks" && dirty && <div className="mb-3"><Notice tone="warning">You have unsaved changes. These checks are from the last save; save the draft to re-check.</Notice></div>}
      {tab === "checks" && (issues.length === 0 ? (dirty ? null : <Notice tone="success">No problems found. This workflow can be published.</Notice>) : (
        <ul className="space-y-2">{issues.map((i, k) => (
          <li key={k}><button type="button" disabled={!i.node_id} onClick={() => i.node_id && onSelect(i.node_id)}
            className={clsx("w-full rounded-md border px-3 py-2 text-left text-xs", i.level === "error" ? "border-danger/30 bg-danger/5" : "border-warning/30 bg-warning/5", i.node_id && "hover:border-primary/50")}>
            <span className={i.level === "error" ? "text-danger" : "text-warning"}>{i.level === "error" ? "Error" : "Warning"}</span>
            {i.node_id && <span className="ml-1.5 font-mono text-muted">{i.node_id}</span>}
            <div className="mt-0.5">{i.message}</div>
          </button></li>
        ))}</ul>
      ))}
      {tab === "variables" && (
        <div className="space-y-3">
          <p className="text-xs leading-relaxed text-muted">Variables are available in every step as <span className="font-mono text-fg">{"{{ vars.name }}"}</span>. Changes apply to new runs immediately, without republishing.</p>
          <Field label="Variables (JSON object)" error={parsed === null ? "Must be a JSON object" : null}><Textarea value={vars} onChange={(e) => setVars(e.target.value)} className="min-h-[180px] font-mono text-xs" spellCheck={false} /></Field>
          <Field label="Run time limit (seconds)" hint="Default 900. Time spent waiting does not count."><Input type="number" min={1} max={86400} value={timeout} onChange={(e) => setTimeoutS(e.target.value)} /></Field>
          <InlineError error={save.error} />
          <Button variant="primary" size="sm" loading={save.isPending} disabled={parsed === null} onClick={() => save.mutate()}>Save variables</Button>
        </div>
      )}
      {tab === "versions" && (versions.isLoading ? <Spinner /> : (versions.data?.items ?? []).length === 0 ? <p className="text-xs text-muted">Not published yet. Publishing creates version 1.</p> : (
        <ul className="space-y-2">{versions.data!.items.map((v) => (
          <li key={v.id} className="rounded-md border px-3 py-2 text-xs">
            <div className="flex items-center justify-between"><span className="font-medium">Version {v.version} {v.is_current && <Badge tone="success" className="ml-1">Live</Badge>}</span>
              <Button size="sm" loading={restore.isPending && restore.variables === v.id} onClick={() => window.confirm(`Replace the current draft with version ${v.version}?`) && restore.mutate(v.id)}>Restore to draft</Button></div>
            <div className="mt-1 text-muted">{fmtDate(v.created_at)} - {v.node_count} steps{v.notes ? ` - ${v.notes}` : ""}</div>
          </li>
        ))}</ul>
      ))}
      <InlineError error={restore.error} />
    </div>
  );
}

function TestDialog({ open, onClose, workflowId, save, onStatus }: { open: boolean; onClose: () => void; workflowId: string; save: () => Promise<void>; onStatus: (s: Record<string, string>) => void }) {
  const [input, setInput] = useState("{}");
  const [execId, setExecId] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [starting, setStarting] = useState(false);
  const exec = useQuery({
    queryKey: ["test-exec", execId], queryFn: () => api(`/executions/${execId}`), enabled: !!execId,
    refetchInterval: (q) => (q.state.data && ["COMPLETED", "FAILED", "CANCELLED", "WAITING"].includes((q.state.data as any).status) ? false : 1000),
  });
  useEffect(() => {
    if (!exec.data) return;
    const map: Record<string, string> = {};
    for (const s of exec.data.steps) map[s.node_id] = s.status;
    onStatus(map);
  }, [exec.data, onStatus]);
  async function start() {
    setError(null);
    let parsed: unknown;
    try { parsed = JSON.parse(input || "{}"); } catch { setError(new Error("Test input must be valid JSON")); return; }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) { setError(new Error("Test input must be a JSON object")); return; }
    setStarting(true);
    try {
      await save();
      const ex = await api<{ id: string }>(`/workflows/${workflowId}/test`, { method: "POST", body: { input: parsed } });
      setExecId(ex.id);
    } catch (e) { setError(e); } finally { setStarting(false); }
  }
  const d = exec.data;
  return (
    <Dialog open={open} onClose={onClose} wide title="Test run" description="Runs the current draft once. Steps perform their real actions (messages are sent, records are created); test runs do not count against quotas."
      footer={<><Button onClick={onClose}>Close</Button><Button variant="primary" loading={starting} onClick={start}><Play className="h-3.5 w-3.5" /> Run test</Button></>}>
      <Field label="Trigger payload (JSON)" hint="Available to steps as {{ trigger.* }}. For webhook-style triggers use {&quot;body&quot;: {...}}.">
        <Textarea value={input} onChange={(e) => setInput(e.target.value)} className="min-h-[90px] font-mono text-xs" spellCheck={false} />
      </Field>
      <InlineError error={error} />
      {execId && (exec.isLoading || !d ? <Spinner label="Starting" /> : (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-sm"><StatusBadge status={d.status} /><span className="text-muted">{fmtDuration(d.duration_ms)}</span>
            <Link href={`/executions/${d.id}`} className="ml-auto text-xs text-primary hover:underline">Open full execution</Link></div>
          {d.error && <Notice tone="warning">{d.error.node_name ? `${d.error.node_name}: ` : ""}{d.error.message}</Notice>}
          {d.status === "WAITING" && <Notice>The run is paused on a wait, delay or approval step. It continues from the Executions page.</Notice>}
          <ul className="divide-y divide-line/60 rounded-md border">{d.steps.map((s: any) => (
            <li key={s.id} className="px-3 py-2">
              <div className="flex items-center gap-2 text-xs"><StatusBadge status={s.status} /><span className="font-medium">{s.node_name || s.node_id}</span>
                <span className="font-mono text-muted">{s.node_id}{s.iteration ? ` [${s.iteration}]` : ""}</span><span className="ml-auto text-muted">{fmtDuration(s.duration_ms)}</span></div>
              {(s.error || s.output) && <details className="mt-1.5"><summary className="cursor-pointer text-2xs uppercase tracking-wider text-muted">{s.error ? "Error" : "Output"}</summary>
                <Code className="mt-1">{JSON.stringify(s.error ?? s.output, null, 2)}</Code></details>}
            </li>
          ))}</ul>
        </div>
      ))}
    </Dialog>
  );
}

function Editor({ workflow, specs, reload }: { workflow: any; specs: NodeSpec[]; reload: () => Promise<unknown> }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const rf = useReactFlow();
  const initial = useMemo(() => toFlow(workflow.definition as Definition), [workflow.id, workflow.draft_revision]); // eslint-disable-line react-hooks/exhaustive-deps
  const [nodes, setNodes, onNodesChange] = useNodesState<WaeNode>(initial.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(initial.edges);
  const [selected, setSelected] = useState<string | null>(null);
  const [sheet, setSheet] = useState<null | "steps" | "details">(null); // small screens: which overlay is open
  const [dirty, setDirty] = useState(false);
  const [differs, setDiffers] = useState<boolean>(Boolean(workflow.has_unpublished_changes));
  useEffect(() => setDiffers(Boolean(workflow.has_unpublished_changes)), [workflow.has_unpublished_changes, workflow.published_version]);
  const [issues, setIssues] = useState<Issue[]>(workflow.issues ?? []);
  const [triggers, setTriggers] = useState<any>(workflow.triggers);
  const [message, setMessage] = useState<{ tone: "success" | "warning"; text: string } | null>(null);
  const [testing, setTesting] = useState(false);
  const [runStatus, setRunStatus] = useState<Record<string, string>>({});
  const revision = useRef<number>(workflow.draft_revision);
  const specMap = useMemo(() => Object.fromEntries(specs.map((s) => [s.type, s])), [specs]);

  useEffect(() => { setNodes(initial.nodes); setEdges(initial.edges); setDirty(false); revision.current = workflow.draft_revision; setIssues(workflow.issues ?? []); setTriggers(workflow.triggers); }, [initial]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const warn = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault(); };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  // Small screens: the settings sheet covers the lower part of the canvas, so centre the selected
  // step in the part that is still visible (keeping the zoom). Large screens have a side panel instead.
  useEffect(() => {
    if (!selected || window.matchMedia("(min-width: 1024px)").matches) return;
    const frame = requestAnimationFrame(() => {
      const n = rf.getNode(selected);
      const canvas = document.querySelector<HTMLElement>(".react-flow")?.getBoundingClientRect();
      const sheet = document.getElementById("builder-panel")?.getBoundingClientRect();
      if (!n || !canvas || !sheet) return;
      const visibleBottom = Math.min(canvas.bottom, sheet.top);
      const offset = canvas.height / 2 - (visibleBottom - canvas.top) / 2; // screen px between canvas centre and visible centre
      const { zoom } = rf.getViewport();
      const w = n.measured?.width ?? 200, h = n.measured?.height ?? 60;
      rf.setCenter(n.position.x + w / 2, n.position.y + h / 2 + offset / zoom, { zoom, duration: 200 });
    });
    return () => cancelAnimationFrame(frame);
  }, [selected, rf]);

  const touch = () => { setDirty(true); setMessage(null); };
  const onConnect = useCallback((c: Connection) => {
    if (!c.source || !c.target || c.source === c.target) return;
    setEdges((eds) => addEdge(makeEdge(c.source!, c.sourceHandle || "out", c.target!), eds.filter((e) => !(e.source === c.source && e.target === c.target && (e.sourceHandle || "out") === (c.sourceHandle || "out")))));
    touch();
  }, [setEdges]);

  const addNode = (spec: NodeSpec) => {
    const id = newNodeId(spec.type, new Set(nodes.map((n) => n.id)));
    const defaults: Record<string, any> = {};
    for (const [k, p] of Object.entries<any>(spec.config_schema.properties ?? {})) if (p.default !== undefined && p.default !== null && typeof p.default !== "object") defaults[k] = p.default;
    // Next to the selected step, otherwise in the middle of the visible canvas; never on top of another step.
    const anchor = nodes.find((n) => n.id === selected);
    let pos: { x: number; y: number };
    if (anchor) {
      pos = { x: anchor.position.x + NODE_W + 80, y: anchor.position.y };
    } else {
      const box = document.querySelector(".react-flow")?.getBoundingClientRect();
      const c = rf.screenToFlowPosition(box ? { x: box.left + box.width / 2, y: box.top + box.height / 2 } : { x: window.innerWidth / 2, y: window.innerHeight / 2 });
      pos = { x: c.x - NODE_W / 2, y: c.y - NODE_H / 2 };
    }
    const overlaps = (p: { x: number; y: number }) => nodes.some((n) => Math.abs(n.position.x - p.x) < NODE_W + 16 && Math.abs(n.position.y - p.y) < NODE_H + 24);
    for (let i = 0; i < 50 && overlaps(pos); i++) pos = { ...pos, y: pos.y + NODE_H + 40 };
    setNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), { id, type: "wae", position: pos, selected: true,
      data: { type: spec.type, name: spec.label, config: defaults, settings: {} } }]);
    // Bring the new step into view if it landed outside the visible canvas.
    const box = document.querySelector(".react-flow")?.getBoundingClientRect();
    if (box) {
      const tl = rf.flowToScreenPosition(pos);
      const br = rf.flowToScreenPosition({ x: pos.x + NODE_W, y: pos.y + NODE_H });
      if (tl.x < box.left || tl.y < box.top || br.x > box.right || br.y > box.bottom) {
        rf.setCenter(pos.x + NODE_W / 2, pos.y + NODE_H / 2, { zoom: rf.getZoom(), duration: 0 });
      }
    }
    setSelected(id);
    touch();
  };
  const patchNode = (id: string, patch: Partial<WaeNode["data"]>) => {
    setNodes((ns) => ns.map((n) => (n.id === id ? { ...n, data: { ...n.data, ...patch } } : n)));
    if (patch.config) {
      // Drop connections whose output no longer exists (e.g. a removed switch case).
      const node = nodes.find((n) => n.id === id);
      const valid = new Set(handlesFor(specMap[node?.data.type ?? ""], patch.config));
      setEdges((eds) => eds.filter((e) => e.source !== id || valid.has(e.sourceHandle || "out")));
    }
    touch();
  };
  const removeNode = (id: string) => { setNodes((ns) => ns.filter((n) => n.id !== id)); setEdges((eds) => eds.filter((e) => e.source !== id && e.target !== id)); setSelected(null); touch(); };
  const duplicate = (id: string) => {
    const src = nodes.find((n) => n.id === id);
    if (!src) return;
    const nid = newNodeId(src.data.type, new Set(nodes.map((n) => n.id)));
    setNodes((ns) => [...ns.map((n) => ({ ...n, selected: false })), { ...src, id: nid, selected: true, position: { x: src.position.x + 40, y: src.position.y + 60 }, data: structuredClone(src.data) }]);
    setSelected(nid);
    touch();
  };

  const saveMut = useMutation({
    mutationFn: () => api(`/workflows/${workflow.id}/draft`, { method: "PUT", body: { definition: toDefinition(nodes, edges), base_revision: revision.current } }),
    onSuccess: (r: any) => { revision.current = r.draft_revision; setIssues(r.issues); setTriggers(r.triggers); setDirty(false); setDiffers(true); qc.invalidateQueries({ queryKey: [key, "workflows"] }); },
  });
  const save = async () => { if (dirty) await saveMut.mutateAsync(); };
  const publish = useMutation({
    mutationFn: async () => { await save(); return api(`/workflows/${workflow.id}/publish`, { method: "POST", body: { notes: "" } }); },
    onSuccess: async (v: any) => { setMessage({ tone: "success", text: `Published version ${v.version}.${workflow.is_active ? " It is live now." : " Activate the workflow to let triggers start it."}` }); await reload(); },
    onError: (e: any) => { if (Array.isArray(e?.details)) setIssues(e.details); },
  });
  const toggle = useMutation({ mutationFn: () => api(`/workflows/${workflow.id}/${workflow.is_active ? "deactivate" : "activate"}`, { method: "POST" }),
    onSuccess: async () => { await reload(); qc.invalidateQueries({ queryKey: [key, "workflows"] }); } });

  const sel = nodes.find((n) => n.id === selected) ?? null;
  const errors = issues.filter((i) => i.level === "error").length;
  const ctx = useMemo(() => ({ specs: specMap, issues, runStatus }), [specMap, issues, runStatus]);
  const failure = saveMut.error ?? publish.error ?? toggle.error;

  return (
    <Ctx.Provider value={ctx}>
      <div className="flex h-full flex-col">
        <header className="flex min-h-14 shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b bg-surface px-4 py-2">
          <Link href="/automations" className="rounded-md p-1.5 text-muted hover:bg-raised hover:text-fg" aria-label="Back to automations"><ArrowLeft className="h-4 w-4" /></Link>
          <div className="min-w-0 flex-1 lg:flex-none">
            <div className="truncate text-sm font-semibold">{workflow.name}</div>
            <div className="flex flex-wrap items-center gap-1.5 text-2xs uppercase tracking-wider text-muted">
              {workflow.published_version ? `Published v${workflow.published_version}` : "Never published"}
              {workflow.is_active ? <Badge tone="success">Active</Badge> : <Badge>Inactive</Badge>}
              {dirty ? <Badge tone="warning">Unsaved changes</Badge> : differs && workflow.published_version && <Badge tone="info">Draft differs from live</Badge>}
            </div>
          </div>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            {errors > 0 && <button type="button" onClick={() => setSelected(null)} className="flex items-center gap-1 text-xs text-danger hover:underline"><AlertTriangle className="h-3.5 w-3.5" /> {errors} {errors === 1 ? "error" : "errors"}</button>}
            <Link href={`/executions?workflow_id=${workflow.id}`}><Button size="sm" variant="ghost"><History className="h-3.5 w-3.5" /> History</Button></Link>
            <Button size="sm" loading={saveMut.isPending} disabled={!dirty} onClick={() => saveMut.mutate()}><Save className="h-3.5 w-3.5" /> Save draft</Button>
            <Button size="sm" onClick={() => setTesting(true)}><FlaskConical className="h-3.5 w-3.5" /> Test</Button>
            {can("workflows.publish") && <Button size="sm" variant="primary" loading={publish.isPending} onClick={() => publish.mutate()}><Rocket className="h-3.5 w-3.5" /> Publish</Button>}
            {can("workflows.activate") && <Button size="sm" loading={toggle.isPending} disabled={!workflow.published_version} title={workflow.published_version ? undefined : "Publish first"} onClick={() => toggle.mutate()}>{workflow.is_active ? "Deactivate" : "Activate"}</Button>}
          </div>
        </header>
        {(failure || message) && (
          <div className={clsx("border-b px-4 py-2 text-xs", failure ? "bg-danger/5 text-danger" : message!.tone === "success" ? "bg-success/5 text-success" : "bg-warning/5 text-warning")} role="status">
            {failure ? errorMessage(failure) : message!.text}
          </div>
        )}
        <div className="flex shrink-0 gap-2 border-b bg-surface px-4 py-2 lg:hidden">
          <Button size="sm" onClick={() => setSheet("steps")}><Plus className="h-3.5 w-3.5" /> Add step</Button>
          <Button size="sm" variant="ghost" onClick={() => setSheet("details")}>Workflow details{errors > 0 ? ` (${errors})` : ""}</Button>
        </div>
        <div className="relative flex min-h-0 flex-1">
          {sheet === "steps" && <div className="fixed inset-0 z-30 bg-black/60 lg:hidden" onClick={() => setSheet(null)} aria-hidden />}
          <Palette specs={specs} open={sheet === "steps"} onClose={() => setSheet(null)} onAdd={(s) => { addNode(s); setSheet(null); }} />
          <div className="min-w-0 flex-1">
            <ReactFlow
              nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView fitViewOptions={{ maxZoom: 1, padding: 0.3 }} minZoom={0.2} colorMode="dark"
              onNodesChange={(c) => { onNodesChange(c); if (c.some((x) => x.type === "position" && x.dragging === false || x.type === "remove")) touch(); }}
              onEdgesChange={(c) => { onEdgesChange(c); if (c.some((x) => x.type === "remove")) touch(); }}
              onConnect={onConnect}
              onNodesDelete={(deleted) => { if (deleted.some((n) => n.id === selected)) setSelected(null); }}
              onSelectionChange={({ nodes: s }) => setSelected(s.length === 1 ? s[0].id : null)}
              deleteKeyCode={["Backspace", "Delete"]} proOptions={{ hideAttribution: false }}
            >
              <Background gap={20} color="rgb(40 37 58)" />
              <Controls showInteractive={false} />
              <MiniMap className="max-sm:!hidden" pannable zoomable nodeColor={(n) => `rgb(${CATEGORY[specMap[(n.data as any).type]?.category ?? "action"].color})`} maskColor="rgb(11 10 16 / 0.7)" />
            </ReactFlow>
          </div>
          {/* A side column on large screens; on smaller ones a bottom sheet for the selected step or the workflow. */}
          <aside id="builder-panel" className={clsx("w-[360px] shrink-0 overflow-y-auto border-l bg-surface",
            sel || sheet === "details" ? "max-lg:fixed max-lg:inset-x-0 max-lg:bottom-0 max-lg:z-40 max-lg:max-h-[55dvh] max-lg:w-full max-lg:rounded-t-xl max-lg:border-l-0 max-lg:border-t max-lg:shadow-2xl" : "max-lg:hidden")}>
            <div className="sticky top-0 z-10 flex items-center justify-between border-b bg-surface px-4 py-2 lg:hidden">
              <span className="text-sm font-medium">{sel ? "Step settings" : "Workflow details"}</span>
              <button type="button" aria-label="Close panel" className="rounded-md p-1.5 text-muted hover:bg-raised hover:text-fg"
                onClick={() => { setSheet(null); setNodes((ns) => ns.map((n) => ({ ...n, selected: false }))); setSelected(null); }}><X className="h-4 w-4" /></button>
            </div>
            {sel ? (
              <NodePanel node={sel} spec={specMap[sel.data.type]} issues={issues.filter((i) => i.node_id === sel.id)} triggers={triggers} workflowId={workflow.id}
                onChange={(p) => patchNode(sel.id, p)} onDelete={() => removeNode(sel.id)} onDuplicate={() => duplicate(sel.id)}
                onTriggersChanged={async () => { const w = await api(`/workflows/${workflow.id}`); setTriggers(w.triggers); }} />
            ) : (
              <WorkflowPanel workflow={workflow} issues={issues} dirty={dirty} onReload={() => reload()}
                onSelect={(id) => { setNodes((ns) => ns.map((n) => ({ ...n, selected: n.id === id }))); setSelected(id); }} />
            )}
          </aside>
        </div>
      </div>
      <TestDialog open={testing} onClose={() => setTesting(false)} workflowId={workflow.id} save={save} onStatus={setRunStatus} />
    </Ctx.Provider>
  );
}

export function Builder({ workflowId }: { workflowId: string }) {
  const key = useOrgKey();
  const { org, can } = useSession();
  const locating = useLocateOrg("workflow", workflowId);
  const wf = useQuery({ queryKey: [key, "workflow", workflowId], queryFn: () => api(`/workflows/${workflowId}`), enabled: !!org });
  const types = useQuery({ queryKey: ["node-types"], queryFn: () => api<{ items: NodeSpec[] }>("/node-types"), enabled: !!org, staleTime: 600_000 });
  if (locating) return <Spinner label="Opening workflow" />;
  if (!org) return <div className="p-8"><Notice tone="warning">Select the organization this workflow belongs to.</Notice></div>;
  if (!can("workflows.write")) return <div className="p-8"><Notice tone="warning">Your role cannot edit workflows.</Notice></div>;
  if (wf.isLoading || types.isLoading) return <Spinner label="Loading builder" />;
  if (wf.error || types.error || !wf.data || !types.data) {
    return <div className="mx-auto mt-16 max-w-md space-y-3 text-center"><InlineError error={wf.error ?? types.error} /><Link href="/automations" className="text-xs text-primary hover:underline">Back to automations</Link></div>;
  }
  return (
    <ReactFlowProvider>
      <Editor workflow={wf.data} specs={types.data.items} reload={() => wf.refetch()} />
    </ReactFlowProvider>
  );
}
