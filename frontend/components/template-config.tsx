"use client";

import { SchemaForm } from "./schema-form";
import { Notice } from "./ui";

/** Install-time configuration of a template. Values become workflow variables. */
export function TemplateConfig({ schema, value, onChange }: { schema: any; value: Record<string, any>; onChange: (v: Record<string, any>) => void }) {
  const props = schema?.properties ?? {};
  if (Object.keys(props).length === 0) return <Notice>This template needs no configuration.</Notice>;
  return <SchemaForm schema={schema} value={value} onChange={onChange} />;
}

/** Read-only outline of a workflow definition for template previews. */
export function DefinitionOutline({ definition }: { definition: { nodes: any[]; edges: any[] } | null }) {
  if (!definition) return null;
  const targets = new Map<string, string[]>();
  for (const e of definition.edges) targets.set(e.source, [...(targets.get(e.source) ?? []), `${e.source_handle && e.source_handle !== "out" ? e.source_handle + " -> " : ""}${e.target}`]);
  return (
    <ol className="divide-y divide-line/60 rounded-md border">
      {definition.nodes.map((n, i) => (
        <li key={n.id} className="flex items-start gap-3 px-3 py-2 text-sm">
          <span className="tabular mt-0.5 w-5 text-xs text-muted">{i + 1}</span>
          <div className="min-w-0 flex-1">
            <div className="font-medium">{n.name || n.id}</div>
            <div className="font-mono text-2xs text-muted">{n.type}</div>
            {targets.get(n.id) && <div className="mt-0.5 text-xs text-muted">then: {targets.get(n.id)!.join(", ")}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}
