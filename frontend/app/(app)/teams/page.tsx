"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Bot, Crown, Users } from "lucide-react";
import { api } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtRelative } from "@/lib/format";
import { Badge, Card, CardHeader, EmptyState, ErrorState, Notice, PageHeader, Spinner } from "@/components/ui";

function Member({ m }: { m: any }) {
  return (
    <li>
      <Link href={`/agents/${m.id}`} className="block rounded-md border bg-bg p-2.5 hover:border-primary/60 hover:bg-raised/40">
        <div className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium"><Bot className="h-3.5 w-3.5 shrink-0 text-muted" /><span className="truncate">{m.name}</span></span>
          {m.status !== "active" && <Badge tone="warning">{m.status}</Badge>}
        </div>
        <p className="mt-0.5 line-clamp-2 text-xs text-muted">{m.description}</p>
        <div className="mt-1.5 flex flex-wrap gap-x-3 text-2xs text-muted">
          <span className="tabular">{m.runs_7d} runs this week</span>
          {m.delegated_7d > 0 && <span className="tabular">{m.delegated_7d} delegated</span>}
          <span>{m.last_run_at ? `last ${fmtRelative(m.last_run_at)}` : "not run yet"}</span>
        </div>
      </Link>
    </li>
  );
}

function Kpi({ label, value, href }: { label: string; value: React.ReactNode; href?: string }) {
  const body = <><div className="text-2xs font-medium uppercase tracking-wider text-muted">{label}</div><div className="tabular mt-1 text-xl font-semibold">{value}</div></>;
  return href ? <Link href={href} className="rounded-lg border bg-surface p-3 hover:border-primary/60">{body}</Link> : <div className="rounded-lg border bg-surface p-3">{body}</div>;
}

export default function TeamsPage() {
  const key = useOrgKey();
  const { org, can } = useSession();
  const teams = useQuery({ queryKey: [key, "teams"], queryFn: () => api<{ items: any[] }>("/teams"), enabled: !!org, refetchInterval: 60_000 });
  if (!org) return <><PageHeader title="AI Teams" /><Notice tone="warning">Select an organization to see its AI teams.</Notice></>;
  return (
    <>
      <PageHeader title="AI Teams" description="Agents installed as a team: a lead that plans and delegates, and specialists that do the work. People approve what goes out." />
      {teams.isLoading ? <Spinner /> : teams.error ? <ErrorState error={teams.error} onRetry={() => teams.refetch()} /> : teams.data!.items.length === 0 ? (
        <Card><EmptyState icon={<Users className="h-5 w-5" />} title="No AI team yet"
          description={can("templates.install") ? <>Install the <Link href="/solutions" className="text-primary hover:underline">AI Marketing Team</Link> from Industry Solutions.</> : "Your Weakless team can install one for you."} /></Card>
      ) : teams.data!.items.map((t) => (
        <div key={t.key} className="space-y-4">
          <h2 className="text-base font-semibold">{t.name}</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
            <Kpi label="Agent runs (7 days)" value={t.kpis.runs_7d} />
            <Kpi label="Delegated tasks" value={t.kpis.delegations_7d} />
            <Kpi label="AI cost (7 days)" value={`$${Number(t.kpis.cost_usd_7d).toFixed(2)}`} />
            <Kpi label="Awaiting approval" value={t.kpis.content_awaiting_approval} href="/content" />
            <Kpi label="Approved" value={t.kpis.content_approved} href="/content" />
            <Kpi label="Published" value={t.kpis.content_published} href="/content" />
          </div>
          {t.lead && (
            <Card className="border-primary/40">
              <CardHeader title={<span className="flex items-center gap-2"><Crown className="h-4 w-4 text-primary" />{t.lead.name}</span> as any} description={t.lead.description} />
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 pb-4 text-xs text-muted">
                <span className="tabular">{t.lead.runs_7d} runs this week</span>
                <span>{t.lead.last_run_at ? `last ${fmtRelative(t.lead.last_run_at)}` : "not run yet"}</span>
                <Link href={`/agents/${t.lead.id}`} className="text-primary hover:underline">Brief the lead</Link>
              </div>
            </Card>
          )}
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {t.modules.map((mod: any) => (
              <Card key={mod.name}>
                <CardHeader title={mod.name} description={`${mod.agents.length} ${mod.agents.length === 1 ? "agent" : "agents"}`} />
                <ul className="space-y-2 px-4 pb-4">{mod.agents.map((m: any) => <Member key={m.id} m={m} />)}</ul>
              </Card>
            ))}
          </div>
        </div>
      ))}
    </>
  );
}
