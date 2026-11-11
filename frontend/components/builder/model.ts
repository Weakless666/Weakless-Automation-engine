import type { Edge, Node } from "@xyflow/react";

export type NodeSpec = {
  type: string;
  category: "trigger" | "action" | "logic" | "ai";
  label: string;
  description: string;
  icon: string;
  config_schema: Record<string, any>;
  output_schema: { properties?: Record<string, any> };
  handles: string[];
  dynamic_handles: { field: string; key: string | null; extra: string[] } | null;
  supports_error_handle: boolean;
  idempotent: boolean;
  providers: string[];
  high_impact: boolean;
};

export type WaeData = { type: string; name: string; config: Record<string, any>; settings: Record<string, any> };
export type WaeNode = Node<WaeData, "wae">;

export type Definition = {
  nodes: { id: string; type: string; name: string; config: Record<string, any>; settings: Record<string, any>; position: { x: number; y: number } }[];
  edges: { source: string; target: string; source_handle: string }[];
};

export const CATEGORY: Record<string, { label: string; color: string }> = {
  trigger: { label: "Triggers", color: "52 211 153" },
  action: { label: "Actions", color: "96 165 250" },
  logic: { label: "Logic", color: "251 191 36" },
  ai: { label: "AI", color: "139 92 246" },
};

/** Outgoing handles of a node - mirrors NodeSpec.handles_for on the backend. */
export function handlesFor(spec: NodeSpec | undefined, config: Record<string, any>): string[] {
  if (!spec) return ["out"];
  let handles = [...spec.handles];
  if (spec.dynamic_handles) {
    const items: any[] = Array.isArray(config?.[spec.dynamic_handles.field]) ? config[spec.dynamic_handles.field] : [];
    const key = spec.dynamic_handles.key;
    const names = items.map((i) => String(key && i && typeof i === "object" ? i[key] ?? "" : i ?? "")).filter((n) => n !== "");
    handles = [...names, ...spec.dynamic_handles.extra];
  }
  if (spec.supports_error_handle && !handles.includes("error")) handles.push("error");
  return Array.from(new Set(handles));
}

export function toFlow(def: Definition): { nodes: WaeNode[]; edges: Edge[] } {
  return {
    nodes: def.nodes.map((n) => ({
      id: n.id,
      type: "wae",
      position: { x: n.position?.x ?? 0, y: n.position?.y ?? 0 },
      data: { type: n.type, name: n.name ?? "", config: n.config ?? {}, settings: n.settings ?? {} },
    })),
    edges: def.edges.map((e) => makeEdge(e.source, e.source_handle || "out", e.target)),
  };
}

export function makeEdge(source: string, handle: string, target: string): Edge {
  return {
    id: `${source}:${handle}->${target}`,
    source,
    target,
    sourceHandle: handle,
    label: handle === "out" ? undefined : handle,
    style: handle === "error" ? { stroke: "rgb(248 113 113)" } : undefined,
  };
}

export function toDefinition(nodes: WaeNode[], edges: Edge[]): Definition {
  return {
    nodes: nodes.map((n) => ({
      id: n.id,
      type: n.data.type,
      name: n.data.name,
      config: n.data.config ?? {},
      settings: n.data.settings ?? {},
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y) },
    })),
    edges: edges.map((e) => ({ source: e.source, target: e.target, source_handle: e.sourceHandle || "out" })),
  };
}

export function newNodeId(type: string, existing: Set<string>): string {
  const base = type.split(".").pop()!.replace(/[^a-z0-9_]/gi, "_").toLowerCase();
  let i = 1;
  while (existing.has(`${base}_${i}`)) i += 1;
  return `${base}_${i}`;
}
