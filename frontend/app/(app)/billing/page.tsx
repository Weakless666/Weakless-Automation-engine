"use client";

import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtCompact, fmtDate, fmtDay, fmtMoney, fmtNumber, fmtUsd, titleCase } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Checkbox, Dialog, ErrorState, Field, InlineError, Input, Notice, PageHeader, Spinner, StatusBadge, Table, Tabs, Td, Textarea, Th } from "@/components/ui";

const QUOTA_LABEL: Record<string, string> = { executions_per_month: "Executions this month", ai_tokens_per_month: "AI tokens this month", workflows: "Workflows", integrations: "Integrations" };
const LIMIT_FIELDS = ["executions_per_month", "ai_tokens_per_month", "workflows", "integrations", "concurrency"];

function Meter({ label, q }: { label: string; q: any }) {
  const pct = q.limit ? Math.min(100, (q.used / q.limit) * 100) : 0;
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm"><span>{label}</span>
        <span className="tabular text-muted">{fmtNumber(Math.round(q.used))} {q.limit === null || q.limit === undefined ? "- unlimited" : `of ${fmtNumber(q.limit)}`}</span></div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-raised" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
        <div className={`h-full rounded-full ${q.exceeded ? "bg-danger" : pct >= 80 ? "bg-warning" : "bg-primary"}`} style={{ width: `${q.limit ? Math.max(pct, 1) : 0}%` }} /></div>
      {q.exceeded && <p className="mt-1 text-xs text-danger">Limit reached. New {label.toLowerCase().includes("execution") ? "executions are rejected" : "items cannot be added"} until the plan changes{label.includes("month") ? " or the month ends" : ""}.</p>}
    </div>
  );
}

function PlanEditor({ plan, onClose }: { plan: any | null; onClose: () => void }) {
  const qc = useQueryClient();
  const blank = { key: "", name: "", description: "", price_monthly_cents: 0, currency: "usd", limits: {}, features: [], provider_price_id: "", is_active: true, is_public: true, sort_order: 10 };
  const [f, setF] = useState<any>(plan?.id ? plan : blank);
  const isNew = !plan?.id;
  const save = useMutation({
    mutationFn: () => {
      const limits = Object.fromEntries(LIMIT_FIELDS.map((k) => [k, f.limits?.[k] === "" || f.limits?.[k] === undefined || f.limits?.[k] === null ? null : Number(f.limits[k])]));
      const body = { key: f.key, name: f.name, description: f.description, price_monthly_cents: Math.round(Number(f.price_monthly_cents) || 0), currency: f.currency, limits,
        features: f.features, provider_price_id: f.provider_price_id || null, is_active: f.is_active, is_public: f.is_public, sort_order: Number(f.sort_order) || 0 };
      return isNew ? api("/billing/plans", { method: "POST", org: false, body }) : api(`/billing/plans/${plan.id}`, { method: "PUT", org: false, body });
    },
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["plans"] }); onClose(); } });
  return (
    <Dialog open onClose={onClose} wide title={isNew ? "New plan" : `Edit ${plan.name}`} description="Changes to limits apply to subscribed organizations immediately."
      footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" loading={save.isPending} disabled={!f.key || !f.name} onClick={() => save.mutate()}>Save plan</Button></>}>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Key" required hint="Stable identifier"><Input value={f.key} disabled={!isNew} onChange={(e) => setF({ ...f, key: e.target.value.toLowerCase() })} /></Field>
        <Field label="Name" required><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        <Field label="Monthly price (cents)"><Input type="number" min={0} value={f.price_monthly_cents} onChange={(e) => setF({ ...f, price_monthly_cents: e.target.value })} /></Field>
        <Field label="Currency"><Input value={f.currency} maxLength={3} onChange={(e) => setF({ ...f, currency: e.target.value.toLowerCase() })} /></Field>
      </div>
      <Field label="Description"><Input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} /></Field>
      <div className="grid grid-cols-3 gap-3">{LIMIT_FIELDS.map((k) => (
        <Field key={k} label={titleCase(k)} hint="Empty = unlimited"><Input type="number" min={0} value={f.limits?.[k] ?? ""} onChange={(e) => setF({ ...f, limits: { ...f.limits, [k]: e.target.value } })} /></Field>))}</div>
      <Field label="Features (one per line)"><Textarea value={(f.features ?? []).join("\n")} onChange={(e) => setF({ ...f, features: e.target.value.split("\n").map((x: string) => x.trim()).filter(Boolean) })} /></Field>
      <Field label="Stripe price id" hint="Required for self-serve checkout of this plan. Leave empty to handle upgrades manually."><Input value={f.provider_price_id ?? ""} onChange={(e) => setF({ ...f, provider_price_id: e.target.value })} placeholder="price_..." /></Field>
      <div className="flex gap-6"><Checkbox label="Active" checked={f.is_active} onChange={(v) => setF({ ...f, is_active: v })} /><Checkbox label="Visible to clients" checked={f.is_public} onChange={(v) => setF({ ...f, is_public: v })} /></div>
      <InlineError error={save.error} />
    </Dialog>
  );
}

function BillingInner() {
  const { org, can, isStaff } = useSession();
  const key = useOrgKey();
  const qc = useQueryClient();
  const checkout = useSearchParams().get("checkout");
  // null = the first tab available for the current selection, so choosing an organization later shows its usage.
  const [chosen, setTab] = useState<"usage" | "plans" | "history" | null>(null);
  const [editing, setEditing] = useState<any | null | undefined>(undefined);
  const [outcome, setOutcome] = useState<string | null>(null);
  const sub = useQuery({ queryKey: [key, "subscription"], queryFn: () => api("/billing/subscription"), enabled: !!org && can("billing.read") });
  const usage = useQuery({ queryKey: [key, "usage"], queryFn: () => api("/usage"), enabled: !!org && can("billing.read") });
  const history = useQuery({ queryKey: [key, "usage-history"], queryFn: () => api<{ items: any[] }>("/usage/history", { params: { months: 12 } }), enabled: !!org && chosen === "history" });
  const events = useQuery({ queryKey: [key, "billing-events"], queryFn: () => api<Page<any>>("/billing/events", { params: { limit: 30 } }), enabled: !!org && chosen === "history" });
  const plans = useQuery({ queryKey: ["plans"], queryFn: () => api<{ items: any[] }>("/billing/plans", { org: false }) });
  const refresh = () => qc.invalidateQueries({ queryKey: [key] });
  const change = useMutation({ mutationFn: (plan: string) => api("/billing/change-plan", { method: "POST", body: { plan } }),
    onSuccess: (r: any) => {
      if (r.result === "checkout" && r.checkout_url) { window.location.href = r.checkout_url; return; }
      setOutcome(r.result === "applied" ? "The new plan is active." : r.result === "scheduled" ? "The change is scheduled for the end of the current billing period." : "Your request was sent to the Weakless team. Nothing was charged and your plan has not changed yet.");
      refresh();
    } });
  const cancel = useMutation({ mutationFn: () => api("/billing/cancel", { method: "POST" }), onSuccess: (r: any) => { setOutcome(`The subscription ends on ${fmtDay(r.effective)}.`); refresh(); } });

  const manage = can("billing.manage");
  const managePlans = can("platform.plans.manage");
  const tabs = [...(org ? [{ key: "usage" as const, label: "Usage" }] : []), { key: "plans" as const, label: "Plans" }, ...(org ? [{ key: "history" as const, label: "History" }] : [])];
  const tab = chosen && tabs.some((t) => t.key === chosen) ? chosen : tabs[0].key;
  const current = sub.data?.plan;
  return (
    <>
      <PageHeader title="Usage & Billing" description={org ? `Plan, quotas and metered usage for ${org.name}.` : "Subscription plans available on the platform."} />
      {checkout && <div className="mb-4"><Notice tone={checkout === "success" ? "success" : "warning"}>{checkout === "success" ? "Checkout completed. The plan updates as soon as the payment provider confirms it." : "Checkout was cancelled. Nothing was charged."}</Notice></div>}
      {outcome && <div className="mb-4"><Notice tone="success">{outcome}</Notice></div>}
      <Tabs tabs={tabs} value={tab} onChange={setTab} />

      {tab === "usage" && org && (sub.isLoading || usage.isLoading ? <Spinner /> : sub.error || usage.error ? <ErrorState error={sub.error ?? usage.error} /> : (
        <div className="grid gap-5 lg:grid-cols-[1fr_340px]">
          <Card><CardHeader title="Plan allowance" description={`Billing month ${usage.data.period}`} />
            <div className="space-y-5 p-4">{Object.entries(QUOTA_LABEL).map(([k, label]) => sub.data.quotas[k] && <Meter key={k} label={label} q={sub.data.quotas[k]} />)}</div>
            <div className="grid grid-cols-3 gap-px border-t bg-line/60 text-sm">
              {[["AI requests", fmtNumber(usage.data.totals.ai_requests)], ["Input / output tokens", `${fmtCompact(usage.data.totals.ai_input_tokens)} / ${fmtCompact(usage.data.totals.ai_output_tokens)}`], ["Estimated AI cost", fmtUsd(usage.data.totals.ai_cost_usd)]].map(([l, v]) => (
                <div key={l} className="bg-surface px-4 py-3"><div className="text-xs text-muted">{l}</div><div className="tabular mt-0.5 font-medium">{v}</div></div>))}
            </div></Card>
          <Card><CardHeader title="Subscription" />
            {!sub.data.subscription ? <p className="p-4 text-sm text-muted">No subscription. Usage is not limited.</p> : (
              <div className="space-y-3 p-4 text-sm">
                <div className="flex items-center justify-between"><span className="text-base font-semibold">{current?.name}</span><StatusBadge status={sub.data.subscription.status} /></div>
                <div className="text-muted">{current?.price_monthly_cents ? `${fmtMoney(current.price_monthly_cents, current.currency)} per month` : "No platform charge"}</div>
                <div className="text-xs text-muted">Current period ends {fmtDay(sub.data.subscription.current_period_end)} - {sub.data.subscription.provider === "stripe" ? "billed through Stripe" : "billed by Weakless"}</div>
                {sub.data.subscription.pending_plan && <Notice>Changes to <strong>{sub.data.subscription.pending_plan}</strong> at the end of this period.</Notice>}
                {sub.data.subscription.cancel_at_period_end && <Notice tone="warning">Cancels at the end of this period.</Notice>}
                {manage && !sub.data.subscription.cancel_at_period_end && sub.data.subscription.status !== "canceled" && (
                  <Button size="sm" variant="danger" loading={cancel.isPending} onClick={() => window.confirm("Cancel the subscription at the end of the current period?") && cancel.mutate()}>Cancel subscription</Button>)}
                <InlineError error={cancel.error} />
              </div>)}
          </Card>
        </div>
      ))}

      {tab === "plans" && (plans.isLoading ? <Spinner /> : plans.error ? <ErrorState error={plans.error} /> : (
        <>
          {org && !isStaff && !sub.data?.payment_provider && <div className="mb-4"><Notice>Online payment is not enabled. Requesting an upgrade notifies the Weakless team; nothing is charged automatically.</Notice></div>}
          {managePlans && <div className="mb-4 flex justify-end"><Button size="sm" onClick={() => setEditing(null)}>New plan</Button></div>}
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {plans.data!.items.map((p) => {
              const isCurrent = current?.id === p.id;
              return (
                <Card key={p.id} className={`flex flex-col p-5 ${isCurrent ? "border-primary/60" : ""}`}>
                  <div className="flex items-start justify-between gap-2"><h2 className="text-base font-semibold">{p.name}</h2>
                    <span className="flex gap-1">{isCurrent && <Badge tone="primary">Current</Badge>}{!p.is_public && <Badge>Internal</Badge>}{!p.is_active && <Badge tone="warning">Inactive</Badge>}</span></div>
                  <div className="tabular mt-2 text-2xl font-semibold">{p.price_monthly_cents ? fmtMoney(p.price_monthly_cents, p.currency) : "-"}<span className="ml-1 text-xs font-normal text-muted">{p.price_monthly_cents ? "/ month" : "no platform charge"}</span></div>
                  <p className="mt-1 min-h-[2.5rem] text-xs text-muted">{p.description}</p>
                  <ul className="mt-3 flex-1 space-y-1 text-xs">
                    {LIMIT_FIELDS.map((k) => <li key={k} className="flex justify-between"><span className="text-muted">{titleCase(k)}</span><span className="tabular">{p.limits[k] === null || p.limits[k] === undefined ? "Unlimited" : fmtCompact(p.limits[k])}</span></li>)}
                    {p.features.map((x: string) => <li key={x} className="pt-1 text-fg/80">{x}</li>)}
                  </ul>
                  <div className="mt-4 flex gap-2">
                    {org && manage && p.is_active && !isCurrent && <Button size="sm" variant="primary" loading={change.isPending && change.variables === p.id}
                      onClick={() => window.confirm(isStaff ? `Set ${org.name} to the ${p.name} plan?` : `Request the ${p.name} plan?`) && change.mutate(p.id)}>{isStaff ? "Apply to organization" : current && p.price_monthly_cents <= current.price_monthly_cents ? "Switch" : sub.data?.payment_provider ? "Upgrade" : "Request upgrade"}</Button>}
                    {managePlans && <Button size="sm" onClick={() => setEditing(p)}>Edit</Button>}
                  </div>
                </Card>
              );
            })}
          </div>
          <div className="mt-3"><InlineError error={change.error} /></div>
        </>
      ))}

      {tab === "history" && org && (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card><CardHeader title="Monthly usage" />
            {history.isLoading ? <Spinner /> : history.error ? <div className="p-4"><ErrorState error={history.error} /></div> : history.data!.items.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">No usage recorded yet.</p> : (
              <Table><thead><tr><Th>Month</Th><Th className="text-right">Executions</Th><Th className="text-right">AI requests</Th><Th className="text-right">Tokens</Th><Th className="text-right">Est. AI cost</Th></tr></thead>
                <tbody>{history.data!.items.map((m) => (
                  <tr key={m.period}><Td className="font-medium">{m.period}</Td><Td className="tabular text-right">{fmtNumber(m.executions ?? 0)}</Td><Td className="tabular text-right">{fmtNumber(m.ai_requests ?? 0)}</Td>
                    <Td className="tabular text-right">{fmtCompact((m.ai_input_tokens ?? 0) + (m.ai_output_tokens ?? 0))}</Td><Td className="tabular text-right">{fmtUsd(m.ai_cost_usd ?? 0)}</Td></tr>))}</tbody></Table>)}
          </Card>
          <Card><CardHeader title="Billing events" />
            {events.isLoading ? <Spinner /> : events.error ? <div className="p-4"><ErrorState error={events.error} /></div> : events.data!.items.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">No billing events yet.</p> : (
              <Table><tbody>{events.data!.items.map((e) => (
                <tr key={e.id}><Td><span className="font-medium">{titleCase(e.type.replace(/^subscription\./, ""))}</span><div className="text-xs text-muted">{Object.entries(e.data ?? {}).filter(([, v]) => v !== null && typeof v !== "object").map(([k, v]) => `${k}: ${v}`).join(" - ")}</div></Td>
                  <Td className="tabular text-right">{e.amount_cents !== null ? fmtMoney(e.amount_cents, e.currency ?? "usd") : ""}</Td><Td className="text-right text-xs text-muted">{fmtDate(e.created_at)}</Td></tr>))}</tbody></Table>)}
          </Card>
        </div>
      )}
      {editing !== undefined && <PlanEditor plan={editing} onClose={() => setEditing(undefined)} />}
    </>
  );
}

export default function BillingPage() {
  return <Suspense fallback={<Spinner />}><BillingInner /></Suspense>;
}
