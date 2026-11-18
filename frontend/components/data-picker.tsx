"use client";

// "Insert data" for workflow step settings: lists what earlier steps, the trigger, variables and
// loops make available (with values from the last test run) and inserts the template at the cursor.

import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import { Braces } from "lucide-react";
import clsx from "clsx";
import { Input, Textarea } from "./ui";

export type DataOption = { group: string; label: string; path: string; sample?: unknown };

function preview(v: unknown): string {
  if (v === undefined) return "";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 40 ? `${s.slice(0, 40)}...` : s;
}

export function DataMenu({ options, onPick, label = "Insert data" }: { options: DataOption[]; onPick: (path: string) => void; label?: string }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") { e.stopPropagation(); setOpen(false); } };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc, true);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc, true); };
  }, [open]);
  const shown = useMemo(() => options.filter((o) => !q || `${o.label} ${o.path}`.toLowerCase().includes(q.toLowerCase())), [options, q]);
  const groups = useMemo(() => shown.reduce((acc: Record<string, DataOption[]>, o) => ({ ...acc, [o.group]: [...(acc[o.group] ?? []), o] }), {}), [shown]);
  if (options.length === 0) return null;
  return (
    <div ref={box} className="relative">
      <button type="button" aria-label={label} title={label} aria-expanded={open} onClick={() => setOpen(!open)}
        className={clsx("flex h-9 w-9 items-center justify-center rounded-md border text-muted hover:bg-raised hover:text-fg", open && "border-primary text-primary")}>
        <Braces className="h-4 w-4" />
      </button>
      {open && (
        <div role="dialog" aria-label="Available data" className="absolute right-0 top-10 z-50 w-80 max-w-[85vw] rounded-md border bg-surface shadow-2xl">
          <div className="border-b p-2"><Input autoFocus value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search data..." aria-label="Search data" className="h-8 text-xs" /></div>
          <div className="max-h-72 overflow-y-auto p-1">
            {Object.entries(groups).map(([group, items]) => (
              <div key={group} className="mb-1">
                <div className="px-2 pb-0.5 pt-1.5 text-2xs font-medium uppercase tracking-wider text-muted">{group}</div>
                {items.map((o) => (
                  <button key={o.path} type="button" onClick={() => { onPick(`{{ ${o.path} }}`); setOpen(false); setQ(""); }}
                    className="flex w-full items-baseline gap-2 rounded px-2 py-1 text-left text-xs hover:bg-raised">
                    <span className="shrink-0 font-medium">{o.label}</span>
                    <span className="min-w-0 flex-1 truncate font-mono text-2xs text-muted">{o.sample !== undefined ? preview(o.sample) : o.path}</span>
                  </button>
                ))}
              </div>
            ))}
            {shown.length === 0 && <p className="px-2 py-4 text-center text-xs text-muted">Nothing matches.</p>}
          </div>
        </div>
      )}
    </div>
  );
}

type Props = { value: string; onChange: (v: string) => void; options: DataOption[]; multiline?: boolean; placeholder?: string; className?: string; "aria-label"?: string };

/** A text input or textarea with an "Insert data" button; the template goes in at the cursor. */
export const DataInput = forwardRef<HTMLInputElement | HTMLTextAreaElement, Props>(function DataInput({ value, onChange, options, multiline, ...rest }, _ref) {
  const el = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  const insert = (text: string) => {
    const node = el.current;
    const start = node?.selectionStart ?? value.length, end = node?.selectionEnd ?? value.length;
    const next = value.slice(0, start) + text + value.slice(end);
    onChange(next);
    requestAnimationFrame(() => { node?.focus(); node?.setSelectionRange(start + text.length, start + text.length); });
  };
  return (
    <div className="flex items-start gap-1.5">
      <div className="min-w-0 flex-1">
        {multiline ? <Textarea ref={el} value={value} onChange={(e) => onChange(e.target.value)} {...rest} />
          : <Input ref={el} value={value} onChange={(e) => onChange(e.target.value)} {...rest} />}
      </div>
      <DataMenu options={options} onPick={insert} />
    </div>
  );
});

/** Flatten a sample value into (path, value) pairs a few levels deep, for the picker. */
export function samplePaths(prefix: string, value: unknown, depth = 0, out: { path: string; sample: unknown }[] = []): { path: string; sample: unknown }[] {
  if (out.length >= 60) return out;
  if (value && typeof value === "object" && !Array.isArray(value) && depth < 3) {
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (k.startsWith("_")) continue;
      const path = /^[A-Za-z_][A-Za-z0-9_]*$/.test(k) ? `${prefix}.${k}` : `${prefix}["${k.replace(/"/g, '\\"')}"]`;
      out.push({ path, sample: v });
      samplePaths(path, v, depth + 1, out);
    }
  } else if (Array.isArray(value) && value.length > 0 && depth < 3 && value[0] && typeof value[0] === "object") {
    samplePaths(`${prefix}[0]`, value[0], depth + 1, out);
  }
  return out;
}
