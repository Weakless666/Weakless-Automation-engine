"use client";

// Renders an editable form from a JSON Schema. Used for workflow node configuration
// (schemas come from the backend node registry) and template configuration.

import { useQuery } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api } from "@/lib/api";
import { useOrgKey } from "@/lib/session";
import { titleCase } from "@/lib/format";
import { Button, Checkbox, Field, Input, Select, Textarea } from "./ui";
import { DataInput, type DataOption } from "./data-picker";

type Schema = Record<string, any>;
type Ctx = { root: Schema; providers?: string[]; data?: DataOption[] };

const LONG_TEXT = new Set(["body", "text", "prompt", "system", "content", "summary", "instructions", "html", "system_instructions", "context", "instruction", "description", "question"]);

function resolve(schema: Schema | undefined, root: Schema): Schema {
  if (!schema) return {};
  if (schema.$ref) {
    const name = String(schema.$ref).split("/").pop()!;
    return resolve(root.$defs?.[name], root);
  }
  if (schema.anyOf) {
    const options = (schema.anyOf as Schema[]).filter((s) => s.type !== "null");
    if (options.length === 1) return { ...resolve(options[0], root), title: schema.title, description: schema.description, default: schema.default, nullable: true };
    return { ...schema, type: "any" };
  }
  if (schema.allOf?.length === 1) return { ...resolve(schema.allOf[0], root), title: schema.title, description: schema.description };
  return schema;
}

function useResources() {
  const key = useOrgKey();
  const enabled = key !== "all";
  const integrations = useQuery({ queryKey: [key, "integrations"], queryFn: () => api<{ items: any[] }>("/integrations"), enabled });
  const agents = useQuery({ queryKey: [key, "agents"], queryFn: () => api<{ items: any[] }>("/agents", { params: { limit: 200 } }), enabled });
  const kbs = useQuery({ queryKey: [key, "kbs"], queryFn: () => api<{ items: any[] }>("/knowledge-bases"), enabled });
  const workflows = useQuery({ queryKey: [key, "workflows", "all"], queryFn: () => api<{ items: any[] }>("/workflows", { params: { limit: 200 } }), enabled });
  return { integrations: integrations.data?.items ?? [], agents: agents.data?.items ?? [], kbs: kbs.data?.items ?? [], workflows: workflows.data?.items ?? [] };
}

/** Free-form value: stays a string (templates like {{ nodes.x.y }}) unless it is valid JSON for a number, boolean, list or object. */
function parseLoose(text: string): unknown {
  const t = text.trim();
  if (t === "") return undefined;
  if (/^(-?\d+(\.\d+)?|true|false|null)$/.test(t) || t.startsWith("[") || (t.startsWith("{") && !t.startsWith("{{"))) {
    try {
      return JSON.parse(t);
    } catch {
      return text;
    }
  }
  return text;
}

function show(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function IntegrationSelect({ value, onChange, providers, integrations }: { value: string; onChange: (v: string) => void; providers?: string[]; integrations: any[] }) {
  const usable = integrations.filter((i) => !providers?.length || providers.includes(i.provider_key));
  const isTemplate = typeof value === "string" && value.includes("{{");
  return (
    <>
      <Select value={isTemplate ? "__template" : value ?? ""} onChange={(e) => onChange(e.target.value === "__template" ? value : e.target.value)}>
        <option value="">Select an integration...</option>
        {isTemplate && <option value="__template">Variable: {value}</option>}
        {usable.map((i) => (
          <option key={i.id} value={i.id}>{i.name} ({i.provider_name}){i.status !== "connected" ? ` - ${i.status.replace(/_/g, " ")}` : ""}</option>
        ))}
      </Select>
      {usable.length === 0 && !isTemplate && (
        <span className="mt-1 block text-xs text-warning">
          No matching integration yet{providers?.length ? ` (${providers.join(", ")})` : ""}. Connect one under Integrations.
        </span>
      )}
    </>
  );
}

function ConditionGroup({ value, onChange }: { value: any; onChange: (v: any) => void }) {
  const group = value && typeof value === "object" ? value : { combinator: "and", conditions: [] };
  const conditions: any[] = group.conditions ?? [];
  const set = (i: number, patch: any) => onChange({ ...group, conditions: conditions.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
  const unary = (op: string) => ["is_empty", "is_not_empty", "is_true", "is_false"].includes(op);
  return (
    <div className="space-y-2 rounded-md border bg-bg p-2.5">
      {conditions.length > 1 && (
        <Select value={group.combinator ?? "and"} onChange={(e) => onChange({ ...group, combinator: e.target.value })} className="h-7 w-40 text-xs">
          <option value="and">All conditions match</option>
          <option value="or">Any condition matches</option>
        </Select>
      )}
      {conditions.map((c, i) => (
        <div key={i} className="grid grid-cols-[1fr_auto] gap-1.5">
          <div className="space-y-1.5">
            <Input placeholder="{{ trigger.body.amount }}" value={show(c.left)} onChange={(e) => set(i, { left: parseLoose(e.target.value) ?? "" })} className="h-8 font-mono text-xs" aria-label="Left value" />
            <div className="grid grid-cols-2 gap-1.5">
              <Select value={c.operator ?? "equals"} onChange={(e) => set(i, { operator: e.target.value })} className="h-8 text-xs" aria-label="Operator">
                {OPERATORS.map((o) => <option key={o} value={o}>{o.replace(/_/g, " ")}</option>)}
              </Select>
              {!unary(c.operator ?? "equals") && (
                <Input placeholder="value" value={show(c.right)} onChange={(e) => set(i, { right: parseLoose(e.target.value) ?? "" })} className="h-8 font-mono text-xs" aria-label="Right value" />
              )}
            </div>
          </div>
          <Button size="sm" variant="ghost" aria-label="Remove condition" onClick={() => onChange({ ...group, conditions: conditions.filter((_, j) => j !== i) })}>
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      ))}
      <Button size="sm" onClick={() => onChange({ ...group, combinator: group.combinator ?? "and", conditions: [...conditions, { left: "", operator: "equals", right: "" }] })}>
        <Plus className="h-3.5 w-3.5" /> Add condition
      </Button>
    </div>
  );
}

const OPERATORS = ["equals", "not_equals", "contains", "not_contains", "starts_with", "ends_with", "gt", "gte", "lt", "lte", "is_empty", "is_not_empty", "is_true", "is_false", "in", "not_in", "matches_regex"];

function Control({ name, schema, value, onChange, ctx, res }: { name: string; schema: Schema; value: any; onChange: (v: any) => void; ctx: Ctx; res: ReturnType<typeof useResources> }) {
  const s = resolve(schema, ctx.root);
  const integrationProviders: string[] | undefined = s["x-integration"] ?? (name === "integration_id" ? ctx.providers : undefined);

  if (s.title === "ConditionGroup" || (s.properties?.conditions && s.properties?.combinator)) return <ConditionGroup value={value} onChange={onChange} />;
  if (name === "integration_id" || s["x-integration"]) return <IntegrationSelect value={value ?? ""} onChange={(v) => onChange(v || undefined)} providers={integrationProviders} integrations={res.integrations} />;
  if (name === "agent_id" || s["x-agent"]) {
    const isVar = typeof value === "string" && value.includes("{{");
    return (
      <Select value={isVar ? "__var" : value ?? ""} onChange={(e) => onChange(e.target.value === "__var" ? value : e.target.value || undefined)}>
        <option value="">Select an agent...</option>
        {isVar && <option value="__var">Variable: {value}</option>}
        {res.agents.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
      </Select>
    );
  }
  if (name === "workflow_id") {
    return (
      <Select value={value ?? ""} onChange={(e) => onChange(e.target.value || undefined)}>
        <option value="">Select a workflow...</option>
        {res.workflows.map((w) => <option key={w.id} value={w.id}>{w.name}</option>)}
      </Select>
    );
  }
  if (name === "knowledge_base_ids") {
    const selected: string[] = Array.isArray(value) ? value : [];
    if (res.kbs.length === 0) return <span className="text-xs text-warning">No knowledge bases yet. Create one under AI Agents.</span>;
    return (
      <div className="space-y-1.5 rounded-md border bg-bg p-2.5">
        {res.kbs.map((k) => (
          <Checkbox key={k.id} label={k.name} checked={selected.includes(k.id)} onChange={(on) => onChange(on ? [...selected, k.id] : selected.filter((x) => x !== k.id))} />
        ))}
      </div>
    );
  }
  if (s.enum) {
    return (
      <Select value={value ?? s.default ?? ""} onChange={(e) => onChange(e.target.value)}>
        {(s.enum as string[]).map((o) => <option key={o} value={o}>{o === "" ? "(default)" : o}</option>)}
      </Select>
    );
  }
  if (s.type === "boolean") return <Checkbox label={s.description ?? titleCase(name)} checked={Boolean(value ?? s.default)} onChange={onChange} />;
  if (s.type === "integer" || s.type === "number") {
    return <Input inputMode="decimal" value={show(value ?? s.default)} onChange={(e) => onChange(parseLoose(e.target.value))} placeholder={s.default !== undefined ? String(s.default) : ""} />;
  }
  if (s.type === "string") {
    const multiline = Boolean(s["x-multiline"] || LONG_TEXT.has(name));
    if (ctx.data?.length) {
      return <DataInput value={value ?? ""} multiline={multiline} options={ctx.data} placeholder={s.default != null ? String(s.default) : ""}
        onChange={(v) => onChange(v === "" && !multiline ? undefined : v)} />;
    }
    if (multiline) return <Textarea value={value ?? ""} onChange={(e) => onChange(e.target.value)} placeholder={s.default ?? ""} />;
    return <Input value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)} placeholder={s.default !== undefined && s.default !== null ? String(s.default) : ""} />;
  }
  if (s.type === "array") {
    const item = resolve(s.items, ctx.root);
    if (item.type === "object" && item.properties) {
      const rows: any[] = Array.isArray(value) ? value : [];
      return (
        <div className="space-y-2">
          {rows.map((row, i) => (
            <div key={i} className="rounded-md border bg-bg p-2.5">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-2xs font-medium uppercase tracking-wider text-muted">{titleCase(name).replace(/s$/, "")} {i + 1}</span>
                <Button size="sm" variant="ghost" aria-label="Remove" onClick={() => onChange(rows.filter((_, j) => j !== i))}><Trash2 className="h-3.5 w-3.5" /></Button>
              </div>
              <SchemaFields schema={item} value={row ?? {}} onChange={(v) => onChange(rows.map((r, j) => (j === i ? v : r)))} ctx={ctx} res={res} />
            </div>
          ))}
          <Button size="sm" onClick={() => onChange([...rows, {}])}><Plus className="h-3.5 w-3.5" /> Add {titleCase(name).replace(/s$/, "").toLowerCase()}</Button>
        </div>
      );
    }
    if (typeof value === "string") return <Input value={value} onChange={(e) => onChange(parseLoose(e.target.value))} className="font-mono text-xs" />;
    const list: unknown[] = Array.isArray(value) ? value : [];
    return (
      <Textarea
        value={list.map((v) => (typeof v === "string" ? v : JSON.stringify(v))).join("\n")}
        onChange={(e) => onChange(e.target.value.split("\n").map((l) => l.trim()).filter(Boolean))}
        placeholder="One per line"
        className="min-h-[64px] font-mono text-xs"
      />
    );
  }
  if (s.type === "object" && s.properties) return <div className="rounded-md border bg-bg p-2.5"><SchemaFields schema={s} value={value ?? {}} onChange={onChange} ctx={ctx} res={res} /></div>;
  if (s.type === "object") return <KeyValues value={value} onChange={onChange} data={ctx.data} />;
  // Untyped ("any") values: free text that may be a template or JSON.
  if (ctx.data?.length) return <DataInput value={show(value)} onChange={(v) => onChange(parseLoose(v))} options={ctx.data} placeholder="Value, template or JSON" className="font-mono text-xs" />;
  return <Input value={show(value)} onChange={(e) => onChange(parseLoose(e.target.value))} placeholder="Value, template or JSON" className="font-mono text-xs" />;
}

function KeyValues({ value, onChange, data }: { value: any; onChange: (v: any) => void; data?: DataOption[] }) {
  if (typeof value === "string") return <Input value={value} onChange={(e) => onChange(parseLoose(e.target.value))} className="font-mono text-xs" />;
  const entries = Object.entries((value as Record<string, unknown>) ?? {});
  const commit = (rows: [string, unknown][]) => onChange(Object.fromEntries(rows));
  return (
    <div className="space-y-1.5">
      {entries.map(([k, v], i) => (
        <div key={i} className="grid grid-cols-[2fr_3fr_auto] gap-1.5">
          <Input value={k} aria-label="Key" placeholder="field" className="h-8 font-mono text-xs" onChange={(e) => commit(entries.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          {data?.length ? <DataInput value={show(v)} aria-label="Value" placeholder="value or {{ template }}" className="h-8 font-mono text-xs" options={data}
            onChange={(nv) => commit(entries.map((r, j) => (j === i ? [r[0], parseLoose(nv) ?? ""] : r)))} />
            : <Input value={show(v)} aria-label="Value" placeholder="value or {{ template }}" className="h-8 font-mono text-xs" onChange={(e) => commit(entries.map((r, j) => (j === i ? [r[0], parseLoose(e.target.value) ?? ""] : r)))} />}
          <Button size="sm" variant="ghost" aria-label="Remove field" onClick={() => commit(entries.filter((_, j) => j !== i))}><Trash2 className="h-3.5 w-3.5" /></Button>
        </div>
      ))}
      <Button size="sm" onClick={() => commit([...entries, [`field${entries.length + 1}`, ""]])}><Plus className="h-3.5 w-3.5" /> Add field</Button>
    </div>
  );
}

function SchemaFields({ schema, value, onChange, ctx, res }: { schema: Schema; value: Record<string, any>; onChange: (v: Record<string, any>) => void; ctx: Ctx; res: ReturnType<typeof useResources> }) {
  const props: Record<string, Schema> = schema.properties ?? {};
  const required: string[] = schema.required ?? [];
  const names = Object.keys(props);
  if (names.length === 0) return <p className="text-xs text-muted">This step has no settings.</p>;
  return (
    <div className="space-y-3">
      {names.map((name) => {
        const s = resolve(props[name], ctx.root);
        const set = (v: any) => {
          const next = { ...value };
          if (v === undefined) delete next[name];
          else next[name] = v;
          onChange(next);
        };
        if (s.type === "boolean") return <div key={name}><Control name={name} schema={props[name]} value={value[name]} onChange={set} ctx={ctx} res={res} /></div>;
        // Lists, objects and condition groups contain buttons and several inputs: render them as a group, not a <label>.
        const compound = s.type === "object" || name === "knowledge_base_ids" || (s.type === "array" && resolve(s.items, ctx.root).type === "object");
        return (
          <Field key={name} group={compound} label={s.title ?? titleCase(name)} hint={s.description} required={required.includes(name)}>
            <Control name={name} schema={props[name]} value={value[name]} onChange={set} ctx={ctx} res={res} />
          </Field>
        );
      })}
    </div>
  );
}

export function SchemaForm({ schema, value, onChange, providers, data }: {
  schema: Schema; value: Record<string, any>; onChange: (v: Record<string, any>) => void; providers?: string[]; data?: DataOption[];
}) {
  const res = useResources();
  return <SchemaFields schema={schema} value={value ?? {}} onChange={onChange} ctx={{ root: schema, providers, data }} res={res} />;
}
