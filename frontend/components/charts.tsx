"use client";

import { Area, AreaChart, Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmtCompact } from "@/lib/format";

const axis = { fontSize: 11, fill: "rgb(150 146 176)" };
const grid = "rgb(40 37 58)";
const tooltip = {
  contentStyle: { background: "rgb(26 24 37)", border: "1px solid rgb(40 37 58)", borderRadius: 8, fontSize: 12, color: "rgb(236 234 246)" },
  labelStyle: { color: "rgb(150 146 176)", marginBottom: 4 },
  cursor: { stroke: "rgb(150 146 176)", strokeOpacity: 0.3 },
};

function day(v: string) {
  const d = new Date(v + "T00:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export type DayPoint = { date: string; total: number; completed: number; failed: number; avg_duration_ms: number | null; ai_tokens: number };

export function ExecutionsChart({ data }: { data: DayPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={240}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: -18, bottom: 0 }}>
        <defs>
          <linearGradient id="ok" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(139 92 246)" stopOpacity={0.35} />
            <stop offset="100%" stopColor="rgb(139 92 246)" stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={grid} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="date" tickFormatter={day} tick={axis} tickLine={false} axisLine={{ stroke: grid }} minTickGap={28} />
        <YAxis tick={axis} tickLine={false} axisLine={false} allowDecimals={false} tickFormatter={fmtCompact} />
        <Tooltip {...tooltip} labelFormatter={(v) => day(String(v))} />
        <Area type="monotone" dataKey="completed" name="Completed" stroke="rgb(139 92 246)" strokeWidth={2} fill="url(#ok)" />
        <Area type="monotone" dataKey="failed" name="Failed" stroke="rgb(248 113 113)" strokeWidth={2} fill="rgb(248 113 113)" fillOpacity={0.08} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function TokensChart({ data }: { data: DayPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
        <CartesianGrid stroke={grid} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="date" tickFormatter={day} tick={axis} tickLine={false} axisLine={{ stroke: grid }} minTickGap={28} />
        <YAxis tick={axis} tickLine={false} axisLine={false} tickFormatter={fmtCompact} />
        <Tooltip {...tooltip} labelFormatter={(v) => day(String(v))} cursor={{ fill: "rgb(150 146 176)", fillOpacity: 0.08 }} />
        <Bar dataKey="ai_tokens" name="AI tokens" fill="rgb(139 92 246)" radius={[3, 3, 0, 0]} maxBarSize={22} />
      </BarChart>
    </ResponsiveContainer>
  );
}

export function DurationChart({ data }: { data: DayPoint[] }) {
  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
        <CartesianGrid stroke={grid} strokeDasharray="3 3" vertical={false} />
        <XAxis dataKey="date" tickFormatter={day} tick={axis} tickLine={false} axisLine={{ stroke: grid }} minTickGap={28} />
        <YAxis tick={axis} tickLine={false} axisLine={false} tickFormatter={(v) => `${fmtCompact(v)}ms`} />
        <Tooltip {...tooltip} labelFormatter={(v) => day(String(v))} />
        <Area type="monotone" connectNulls dataKey="avg_duration_ms" name="Avg duration (ms)" stroke="rgb(96 165 250)" strokeWidth={2} fill="rgb(96 165 250)" fillOpacity={0.1} />
      </AreaChart>
    </ResponsiveContainer>
  );
}
