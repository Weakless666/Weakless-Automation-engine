"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, Plus, Trash2 } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtRelative } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Checkbox, Dialog, EmptyState, ErrorState, Field, InlineError, Input, Notice, PageHeader, Pager, Select, Spinner, StatusBadge, Table, Tabs, Td, Th } from "@/components/ui";

const TONE: Record<string, string> = { error: "danger", warning: "warning", success: "success", info: "info" };

function link(n: any): string | null {
  if (n.data?.execution_id) return `/executions/${n.data.execution_id}`;
  if (n.data?.approval_id) return "/executions";
  if (n.data?.integration_id) return "/integrations";
  if (n.data?.quota || n.data?.plan) return "/billing";
  return null;
}

function Inbox() {
  const qc = useQueryClient();
  const { me, selectOrg } = useSession();
  const [unread, setUnread] = useState(false);
  const [offset, setOffset] = useState(0);
  const list = useQuery({ queryKey: ["notifications", "list", unread, offset], queryFn: () => api<Page<any> & { unread: number }>("/notifications", { org: false, params: { unread, limit: 25, offset } }), refetchInterval: 20_000 });
  const refresh = () => qc.invalidateQueries({ queryKey: ["notifications"] });
  const read = useMutation({ mutationFn: (id: string) => api(`/notifications/${id}/read`, { method: "POST", org: false }), onSuccess: refresh });
  const readAll = useMutation({ mutationFn: () => api("/notifications/read-all", { method: "POST", org: false }), onSuccess: refresh });
  const orgName = (id: string | null) => me.organizations.find((o) => o.id === id)?.name;
  return (
    <Card>
      <div className="flex items-center justify-between border-b p-3">
        <Checkbox label="Unread only" checked={unread} onChange={(v) => { setUnread(v); setOffset(0); }} />
        <Button size="sm" loading={readAll.isPending} disabled={!list.data?.unread} onClick={() => readAll.mutate()}>Mark all as read</Button>
      </div>
      {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
        <EmptyState icon={<Bell className="h-5 w-5" />} title={unread ? "You are all caught up" : "No notifications yet"} description="Failures, approvals, usage alerts and security events appear here." />
      ) : (
        <>
          <ul className="divide-y divide-line/60">{list.data!.items.map((n) => {
            const href = link(n);
            return (
              <li key={n.id} className={`flex items-start gap-3 px-4 py-3 ${n.read ? "opacity-70" : ""}`}>
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${n.read ? "bg-line" : "bg-primary"}`} aria-label={n.read ? "Read" : "Unread"} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2"><span className="text-sm font-medium">{n.title}</span><Badge tone={TONE[n.severity] ?? "neutral"}>{n.event_type.replace(".", " ")}</Badge></div>
                  {n.body && <p className="mt-0.5 line-clamp-2 text-sm text-muted">{n.body}</p>}
                  <div className="mt-1 text-xs text-muted">{orgName(n.organization_id) ? `${orgName(n.organization_id)} - ` : ""}{fmtRelative(n.created_at)}
                    {href && <> - <Link href={href} className="text-primary hover:underline" onClick={() => { if (n.organization_id && orgName(n.organization_id)) selectOrg(n.organization_id); if (!n.read) read.mutate(n.id); }}>Open</Link></>}</div>
                </div>
                {!n.read && <Button size="sm" variant="ghost" onClick={() => read.mutate(n.id)}>Mark read</Button>}
              </li>
            );
          })}</ul>
          <Pager total={list.data!.total} limit={25} offset={offset} onChange={setOffset} />
        </>
      )}
    </Card>
  );
}

function Preferences() {
  const qc = useQueryClient();
  const prefs = useQuery({ queryKey: ["notification-prefs"], queryFn: () => api<{ items: any[] }>("/notifications/preferences", { org: false }) });
  const save = useMutation({ mutationFn: (item: any) => api("/notifications/preferences", { method: "PUT", org: false, body: { items: [{ event_type: item.event_type, in_app: item.in_app, email: item.email }] } }),
    onSuccess: (d) => qc.setQueryData(["notification-prefs"], d) });
  if (prefs.isLoading) return <Spinner />;
  if (prefs.error) return <ErrorState error={prefs.error} />;
  return (
    <Card><CardHeader title="My preferences" description="Which events reach you, and how. Email is delivered when platform SMTP is configured." />
      <Table><thead><tr><Th>Event</Th><Th className="w-32 text-center">In-app</Th><Th className="w-32 text-center">Email</Th></tr></thead>
        <tbody>{prefs.data!.items.map((p) => (
          <tr key={p.event_type}><Td className="font-medium">{p.label}</Td>
            <Td className="text-center"><input type="checkbox" aria-label={`${p.label} in-app`} className="h-4 w-4 accent-[rgb(var(--primary))]" checked={p.in_app} onChange={(e) => save.mutate({ ...p, in_app: e.target.checked })} /></Td>
            <Td className="text-center"><input type="checkbox" aria-label={`${p.label} by email`} className="h-4 w-4 accent-[rgb(var(--primary))]" checked={p.email} onChange={(e) => save.mutate({ ...p, email: e.target.checked })} /></Td></tr>))}</tbody></Table>
      <div className="px-4 py-2"><InlineError error={save.error} /></div>
    </Card>
  );
}

const CHANNEL_FIELDS: Record<string, { name: string; label: string; secret?: boolean; hint?: string }[]> = {
  webhook: [{ name: "url", label: "Webhook URL (https)", secret: true }, { name: "signing_secret", label: "Signing secret (optional)", secret: true, hint: "Requests carry X-WAE-Signature: sha256=HMAC(secret, body)." }],
  slack: [{ name: "url", label: "Slack incoming webhook URL", secret: true }],
  discord: [{ name: "url", label: "Discord webhook URL", secret: true }],
  telegram: [{ name: "bot_token", label: "Bot token", secret: true }, { name: "chat_id", label: "Chat id" }],
  email: [{ name: "address", label: "Email address" }],
};

function Channels() {
  const key = useOrgKey();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [f, setF] = useState<any>({ kind: "slack", name: "", values: {}, events: [] });
  const [tested, setTested] = useState<Record<string, string>>({});
  const list = useQuery({ queryKey: [key, "channels"], queryFn: () => api<{ items: any[]; event_types: any[] }>("/notification-channels") });
  const log = useQuery({ queryKey: [key, "deliveries"], queryFn: () => api<Page<any>>("/notification-deliveries", { params: { limit: 15 } }) });
  const refresh = () => { qc.invalidateQueries({ queryKey: [key, "channels"] }); qc.invalidateQueries({ queryKey: [key, "deliveries"] }); };
  const create = useMutation({ mutationFn: () => api("/notification-channels", { method: "POST", body: { ...f, values: Object.fromEntries(Object.entries(f.values).filter(([, v]) => v)) } }),
    onSuccess: () => { refresh(); setOpen(false); setF({ kind: "slack", name: "", values: {}, events: [] }); } });
  const remove = useMutation({ mutationFn: (id: string) => api(`/notification-channels/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const test = useMutation({ mutationFn: (id: string) => api(`/notification-channels/${id}/test`, { method: "POST" }),
    onSuccess: (r: any, id) => { setTested((t) => ({ ...t, [id]: r.status === "sent" ? "Delivered" : `Failed: ${r.error}` })); refresh(); } });
  const toggle = useMutation({ mutationFn: (c: any) => api(`/notification-channels/${c.id}`, { method: "PUT", body: { kind: c.kind, name: c.name, values: {}, events: c.events, is_active: !c.is_active } }), onSuccess: refresh });
  return (
    <div className="space-y-5">
      <Card><CardHeader title="Organization channels" description="Send this organization's events to a team chat, an inbox or your own system."
        actions={<Button size="sm" variant="primary" onClick={() => setOpen(true)}><Plus className="h-3.5 w-3.5" /> Add channel</Button>} />
        {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} /></div> : list.data!.items.length === 0 ? <p className="px-4 py-8 text-center text-sm text-muted">No channels configured.</p> : (
          <Table><thead><tr><Th>Channel</Th><Th>Events</Th><Th>State</Th><Th /></tr></thead>
            <tbody>{list.data!.items.map((c) => (
              <tr key={c.id}><Td><span className="font-medium">{c.name}</span><div className="text-xs capitalize text-muted">{c.kind}</div></Td>
                <Td className="max-w-xs text-xs text-muted">{c.events.length ? c.events.join(", ") : "All events"}</Td>
                <Td>{c.is_active ? <Badge tone="success">On</Badge> : <Badge>Off</Badge>}{tested[c.id] && <div className={`mt-1 text-xs ${tested[c.id] === "Delivered" ? "text-success" : "text-danger"}`}>{tested[c.id]}</div>}</Td>
                <Td><div className="flex justify-end gap-1.5"><Button size="sm" loading={test.isPending && test.variables === c.id} onClick={() => test.mutate(c.id)}>Send test</Button>
                  <Button size="sm" onClick={() => toggle.mutate(c)}>{c.is_active ? "Turn off" : "Turn on"}</Button>
                  <Button size="sm" variant="danger" aria-label={`Delete ${c.name}`} onClick={() => window.confirm(`Delete channel "${c.name}"?`) && remove.mutate(c.id)}><Trash2 className="h-3.5 w-3.5" /></Button></div></Td></tr>))}</tbody></Table>)}
        <div className="px-4 py-2"><InlineError error={remove.error ?? test.error ?? toggle.error} /></div>
      </Card>
      <Card><CardHeader title="Recent deliveries" />
        {log.isLoading ? <Spinner /> : (log.data?.items ?? []).length === 0 ? <p className="px-4 py-6 text-center text-sm text-muted">Nothing delivered yet.</p> : (
          <Table><tbody>{log.data!.items.map((d) => (
            <tr key={d.id}><Td className="capitalize">{d.channel_kind}</Td><Td className="text-xs text-muted">{d.event_type}</Td><Td><StatusBadge status={d.status} /></Td>
              <Td className="max-w-xs truncate text-xs text-danger">{d.error}</Td><Td className="text-right text-xs text-muted">{fmtRelative(d.created_at)}</Td></tr>))}</tbody></Table>)}
      </Card>
      <Dialog open={open} onClose={() => setOpen(false)} title="Add notification channel" description="Addresses and tokens are stored encrypted and are not shown again."
        footer={<><Button onClick={() => setOpen(false)}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!f.name.trim()} onClick={() => create.mutate()}>Add channel</Button></>}>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Type"><Select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value, values: {} })}>{Object.keys(CHANNEL_FIELDS).map((k) => <option key={k} value={k}>{k.charAt(0).toUpperCase() + k.slice(1)}</option>)}</Select></Field>
          <Field label="Name" required><Input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></Field>
        </div>
        {CHANNEL_FIELDS[f.kind].map((c) => <Field key={c.name} label={c.label} hint={c.hint}><Input type={c.secret ? "password" : "text"} autoComplete="off" value={f.values[c.name] ?? ""} onChange={(e) => setF({ ...f, values: { ...f.values, [c.name]: e.target.value } })} /></Field>)}
        {f.kind === "email" && <Notice>Email channels use the platform SMTP server. If it is not configured, deliveries are logged as skipped.</Notice>}
        <div><div className="mb-1.5 text-xs font-medium text-muted">Events (none selected = all)</div>
          <div className="grid grid-cols-2 gap-1.5">{(list.data?.event_types ?? []).map((e) => <Checkbox key={e.key} label={e.label} checked={f.events.includes(e.key)} onChange={(on) => setF({ ...f, events: on ? [...f.events, e.key] : f.events.filter((x: string) => x !== e.key) })} />)}</div></div>
        <InlineError error={create.error} />
      </Dialog>
    </div>
  );
}

export default function NotificationsPage() {
  const { org, can } = useSession();
  const [tab, setTab] = useState<"inbox" | "prefs" | "channels">("inbox");
  const showChannels = !!org && can("notifications.manage");
  return (
    <>
      <PageHeader title="Notifications" />
      <Tabs tabs={[{ key: "inbox", label: "Inbox" }, { key: "prefs", label: "Preferences" }, ...(showChannels ? [{ key: "channels" as const, label: "Channels" }] : [])]} value={tab === "channels" && !showChannels ? "inbox" : tab} onChange={setTab} />
      {tab === "prefs" ? <Preferences /> : tab === "channels" && showChannels ? <Channels /> : <Inbox />}
    </>
  );
}
