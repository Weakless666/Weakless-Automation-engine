"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarDays, ChevronLeft, ChevronRight, Plus } from "lucide-react";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDate, fmtRelative } from "@/lib/format";
import { Badge, Button, Card, Dialog, EmptyState, ErrorState, Field, InlineError, Input, Notice, PageHeader, Select, Spinner, Tabs, Textarea } from "@/components/ui";

const CHANNELS = ["linkedin", "instagram", "facebook", "tiktok", "youtube", "x", "blog", "newsletter", "email", "other"];
const CHANNEL_LABEL: Record<string, string> = { linkedin: "LinkedIn", instagram: "Instagram", facebook: "Facebook", tiktok: "TikTok", youtube: "YouTube", x: "X",
  blog: "Blog", newsletter: "Newsletter", email: "Email", other: "Other" };
const STATUS_TONE: Record<string, any> = { idea: "default", draft: "default", awaiting_approval: "warning", approved: "info", published: "success", rejected: "danger", failed: "danger" };
const label = (s: string) => s.replace(/_/g, " ");

type Item = any;

function startOfWeek(d: Date): Date {
  const x = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); // Monday
  return x;
}
const dayKey = (d: Date) => `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
const toLocalInput = (iso?: string | null) => {
  if (!iso) return "";
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};

function ItemCard({ item, onOpen }: { item: Item; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} className="w-full rounded-md border bg-bg p-2 text-left hover:border-primary/60 hover:bg-raised/40">
      <div className="flex items-center justify-between gap-2">
        <span className="text-2xs font-medium uppercase tracking-wider text-muted">{CHANNEL_LABEL[item.channel] ?? item.channel}</span>
        {item.scheduled_for && <span className="tabular text-2xs text-muted">{new Date(item.scheduled_for).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>}
      </div>
      <div className="mt-0.5 line-clamp-2 text-xs font-medium">{item.title}</div>
      <Badge tone={STATUS_TONE[item.status]} className="mt-1">{label(item.status)}</Badge>
    </button>
  );
}

function ItemDialog({ item, creating, onClose }: { item: Item | null; creating: boolean; onClose: () => void }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const editable = creating || (item && ["idea", "draft", "awaiting_approval", "rejected", "failed"].includes(item.status));
  const [form, setForm] = useState<any>({});
  const [note, setNote] = useState("");
  const [url, setUrl] = useState("");
  useEffect(() => {
    setForm(item ? { title: item.title, channel: item.channel, body: item.body, media_notes: item.media_notes, scheduled_for: toLocalInput(item.scheduled_for) }
      : { title: "", channel: "linkedin", body: "", media_notes: "", scheduled_for: "", status: "awaiting_approval" });
    setNote(""); setUrl("");
  }, [item, creating]);
  const done = () => { qc.invalidateQueries({ queryKey: [key, "content"] }); qc.invalidateQueries({ queryKey: [key, "teams"] }); onClose(); };
  const payload = () => ({ ...form, scheduled_for: form.scheduled_for ? new Date(form.scheduled_for).toISOString() : null });
  const save = useMutation({ mutationFn: () => creating ? api("/content", { method: "POST", body: payload() }) : api(`/content/${item.id}`, { method: "PATCH", body: payload() }), onSuccess: done });
  const decide = useMutation({ mutationFn: (approve: boolean) => api(`/content/${item.id}/${approve ? "approve" : "reject"}`, { method: "POST", body: { note } }), onSuccess: done });
  const outcome = useMutation({ mutationFn: (status: string) => api(`/content/${item.id}/outcome`, { method: "POST", body: { status, url: url || null, note } }), onSuccess: done });
  const remove = useMutation({ mutationFn: () => api(`/content/${item.id}`, { method: "DELETE" }), onSuccess: done });
  const open = creating || !!item;
  const canDecide = item && ["awaiting_approval", "draft", "idea"].includes(item.status) && can("content.approve");
  const canWrite = can("content.write");
  const error = save.error ?? decide.error ?? outcome.error ?? remove.error;
  return (
    <Dialog open={open} onClose={onClose} wide title={creating ? "New content" : item?.title ?? ""}
      description={item ? `${CHANNEL_LABEL[item.channel] ?? item.channel} - ${label(item.status)}${item.scheduled_for ? ` - ${fmtDate(item.scheduled_for)}` : " - not scheduled"}` : undefined}
      footer={<>
        {item && canWrite && item.status !== "published" && <Button variant="ghost" loading={remove.isPending} onClick={() => window.confirm("Delete this item?") && remove.mutate()}>Delete</Button>}
        <Button onClick={onClose}>Close</Button>
        {editable && canWrite && <Button loading={save.isPending} disabled={!form.title?.trim()} onClick={() => save.mutate()}>{creating ? "Add to calendar" : "Save changes"}</Button>}
        {canDecide && <><Button variant="danger" loading={decide.isPending && decide.variables === false} onClick={() => decide.mutate(false)}>Reject</Button>
          <Button variant="primary" loading={decide.isPending && decide.variables === true} onClick={() => decide.mutate(true)}>Approve</Button></>}
      </>}>
      {item?.created_by_agent_id && <Notice>Proposed by an AI agent. Nothing is published until a person approves it.</Notice>}
      {item?.status === "approved" && <Notice tone="info">{item.scheduled_for && new Date(item.scheduled_for) > new Date() ? `Approved. It goes to the publishing workflow ${fmtRelative(item.scheduled_for)}.` : "Approved and handed to the publishing workflow."} After it is live, record the outcome below.</Notice>}
      {item?.status === "published" && <Notice tone="success">Published {fmtRelative(item.published_at)}{item.published_url && <> - <a className="underline" href={item.published_url} target="_blank" rel="noreferrer noopener">open</a></>}.</Notice>}
      {item?.decision_note && <p className="text-xs text-muted">Note: {item.decision_note}</p>}
      {editable && canWrite ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sm:col-span-2"><Field label="Title" required><Input value={form.title ?? ""} onChange={(e) => setForm({ ...form, title: e.target.value })} /></Field></div>
          <Field label="Channel"><Select value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>{CHANNELS.map((c) => <option key={c} value={c}>{CHANNEL_LABEL[c]}</option>)}</Select></Field>
          <Field label="Publish at" hint="Your local time"><Input type="datetime-local" value={form.scheduled_for ?? ""} onChange={(e) => setForm({ ...form, scheduled_for: e.target.value })} /></Field>
          <div className="sm:col-span-2"><Field label="Text"><Textarea className="min-h-[160px]" value={form.body ?? ""} onChange={(e) => setForm({ ...form, body: e.target.value })} /></Field></div>
          <div className="sm:col-span-2"><Field label="Media brief" hint="What image or video goes with it"><Textarea value={form.media_notes ?? ""} onChange={(e) => setForm({ ...form, media_notes: e.target.value })} /></Field></div>
        </div>
      ) : item && (
        <div className="space-y-3">
          <div className="whitespace-pre-wrap rounded-md border bg-bg p-3 text-sm">{item.body || <span className="text-muted">No text</span>}</div>
          {item.media_notes && <div><div className="text-2xs font-medium uppercase tracking-wider text-muted">Media brief</div><p className="text-sm">{item.media_notes}</p></div>}
        </div>
      )}
      {item?.hashtags?.length > 0 && <div className="flex flex-wrap gap-1">{item.hashtags.map((h: string) => <Badge key={h}>#{h}</Badge>)}</div>}
      {canDecide && <Field label="Note (optional)" hint="Shown with the decision; agents read it when they plan again."><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>}
      {item && ["approved", "failed"].includes(item.status) && canWrite && (
        <div className="flex flex-wrap items-end gap-2 rounded-md border p-3">
          <div className="min-w-0 flex-1"><Field label="Link to the live post"><Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://..." /></Field></div>
          <Button loading={outcome.isPending && outcome.variables === "failed"} onClick={() => outcome.mutate("failed")}>Mark failed</Button>
          <Button variant="primary" loading={outcome.isPending && outcome.variables === "published"} onClick={() => outcome.mutate("published")}>Mark published</Button>
        </div>
      )}
      <InlineError error={error} />
    </Dialog>
  );
}

export default function ContentPage() {
  const key = useOrgKey();
  const { org, can } = useSession();
  const [tab, setTab] = useState("calendar");
  const [week, setWeek] = useState(() => startOfWeek(new Date()));
  const [open, setOpen] = useState<Item | null>(null);
  const [creating, setCreating] = useState(false);
  const end = useMemo(() => { const e = new Date(week); e.setDate(e.getDate() + 7); return e; }, [week]);
  const calendar = useQuery({ queryKey: [key, "content", "week", week.toISOString()], enabled: !!org && tab === "calendar",
    queryFn: () => api<{ items: Item[] }>("/content", { params: { start: week.toISOString(), end: end.toISOString() } }) });
  const awaiting = useQuery({ queryKey: [key, "content", "awaiting"], enabled: !!org,
    queryFn: () => api<{ items: Item[] }>("/content", { params: { status: "awaiting_approval" } }), refetchInterval: 30_000 });
  const recent = useQuery({ queryKey: [key, "content", "recent"], enabled: !!org && tab === "all", queryFn: () => api<{ items: Item[] }>("/content") });
  const days = Array.from({ length: 7 }, (_, i) => { const d = new Date(week); d.setDate(d.getDate() + i); return d; });
  const byDay = useMemo(() => {
    const m: Record<string, Item[]> = {};
    for (const it of calendar.data?.items ?? []) if (it.scheduled_for) (m[dayKey(new Date(it.scheduled_for))] ??= []).push(it);
    return m;
  }, [calendar.data]);
  const unscheduled = (calendar.data?.items ?? []).filter((i) => !i.scheduled_for);
  const shift = (n: number) => { const d = new Date(week); d.setDate(d.getDate() + n * 7); setWeek(d); };
  const today = dayKey(new Date());

  if (!org) return <><PageHeader title="Content" /><Notice tone="warning">Select an organization to see its content calendar.</Notice></>;
  return (
    <>
      <PageHeader title="Content" description="Posts, emails and articles planned by your team and AI agents. Nothing is published until it is approved."
        actions={can("content.write") && <Button variant="primary" onClick={() => setCreating(true)}><Plus className="h-3.5 w-3.5" /> New content</Button>} />
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "calendar", label: "Calendar" }, { key: "approval", label: "Awaiting approval", count: awaiting.data?.items.length }, { key: "all", label: "Recently changed" }]} />
      <div className="mt-4">
        {tab === "calendar" && (
          <Card>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
              <div className="flex items-center gap-1">
                <Button size="sm" variant="ghost" aria-label="Previous week" onClick={() => shift(-1)}><ChevronLeft className="h-4 w-4" /></Button>
                <Button size="sm" variant="ghost" aria-label="Next week" onClick={() => shift(1)}><ChevronRight className="h-4 w-4" /></Button>
                <span className="ml-1 text-sm font-medium" aria-live="polite">Week of {week.toLocaleDateString([], { day: "numeric", month: "long", year: "numeric" })}</span>
              </div>
              <Button size="sm" onClick={() => setWeek(startOfWeek(new Date()))}>This week</Button>
            </div>
            {calendar.isLoading ? <Spinner /> : calendar.error ? <div className="p-4"><ErrorState error={calendar.error} /></div> : (
              <>
                <div className="grid gap-px bg-line/40 sm:grid-cols-7">{days.map((d) => (
                  <section key={dayKey(d)} aria-label={d.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" })} className="bg-surface p-2 sm:min-h-[120px]">
                    <div className={clsx("mb-1.5 text-xs", dayKey(d) === today ? "font-semibold text-primary" : "text-muted")}>{d.toLocaleDateString([], { weekday: "short", day: "numeric" })}</div>
                    <div className="space-y-1.5">{(byDay[dayKey(d)] ?? []).map((it) => <ItemCard key={it.id} item={it} onOpen={() => setOpen(it)} />)}</div>
                  </section>))}
                </div>
                {unscheduled.length > 0 && (
                  <div className="border-t p-3"><div className="mb-2 text-2xs font-medium uppercase tracking-wider text-muted">Not scheduled</div>
                    <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-4">{unscheduled.map((it) => <ItemCard key={it.id} item={it} onOpen={() => setOpen(it)} />)}</div></div>
                )}
              </>
            )}
          </Card>
        )}
        {tab !== "calendar" && (() => {
          const q = tab === "approval" ? awaiting : recent;
          return (
            <Card>
              {q.isLoading ? <Spinner /> : q.error ? <div className="p-4"><ErrorState error={q.error} /></div> : q.data!.items.length === 0 ? (
                <EmptyState icon={<CalendarDays className="h-5 w-5" />} title={tab === "approval" ? "Nothing is waiting for approval" : "No content yet"}
                  description="Install the AI Marketing Team from Industry Solutions, or add content yourself." />
              ) : (
                <ul className="divide-y divide-line/60">{q.data!.items.map((it) => (
                  <li key={it.id}><button type="button" onClick={() => setOpen(it)} className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3 text-left hover:bg-raised/40">
                    <Badge tone={STATUS_TONE[it.status]}>{label(it.status)}</Badge>
                    <span className="min-w-0 flex-1 truncate text-sm font-medium">{it.title}</span>
                    <span className="text-xs text-muted">{CHANNEL_LABEL[it.channel] ?? it.channel} - {it.scheduled_for ? fmtDate(it.scheduled_for) : "not scheduled"}</span>
                  </button></li>))}</ul>
              )}
            </Card>
          );
        })()}
      </div>
      <ItemDialog item={open} creating={creating} onClose={() => { setOpen(null); setCreating(false); }} />
    </>
  );
}
