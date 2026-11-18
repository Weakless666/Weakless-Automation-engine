"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarDays, ChevronLeft, ChevronRight, ImagePlus, Plus, Send, X } from "lucide-react";
import clsx from "clsx";
import { api, apiUpload } from "@/lib/api";
import { MediaPreview, type FileRef } from "@/components/media-preview";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDate, fmtRelative } from "@/lib/format";
import { Badge, Button, Card, Dialog, EmptyState, ErrorState, Field, InlineError, Input, Notice, PageHeader, Select, Spinner, Tabs, Textarea } from "@/components/ui";

const CHANNELS = ["linkedin", "instagram", "facebook", "tiktok", "youtube", "x", "blog", "newsletter", "email", "other"];
const CHANNEL_LABEL: Record<string, string> = { linkedin: "LinkedIn", instagram: "Instagram", facebook: "Facebook", tiktok: "TikTok", youtube: "YouTube", x: "X",
  blog: "Blog", newsletter: "Newsletter", email: "Email", other: "Other" };
const STATUS_TONE: Record<string, any> = { idea: "default", draft: "default", awaiting_approval: "warning", approved: "info", published: "success", rejected: "danger", failed: "danger" };
const label = (s: string) => s.replace(/_/g, " ");
type Channel = { channel: string; native: boolean; providers: string[]; integration_id: string | null; integration_name: string | null; integration_status: string | null };
const isMedia = (type: string) => type.startsWith("image/") || type.startsWith("video/");

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
      <div className="mt-1 flex items-center gap-1.5"><Badge tone={STATUS_TONE[item.status]}>{label(item.status)}</Badge>{item.media?.length > 0 && <ImagePlus className="h-3 w-3 text-muted" aria-label="Has media" />}</div>
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
    setForm(item ? { title: item.title, channel: item.channel, body: item.body, media_notes: item.media_notes, scheduled_for: toLocalInput(item.scheduled_for), media: item.media ?? [] }
      : { title: "", channel: "linkedin", body: "", media_notes: "", scheduled_for: "", status: "awaiting_approval", media: [] });
    setNote(""); setUrl(""); publish.reset(); upload.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reset only when another item is opened
  }, [item, creating]);
  const done = () => { qc.invalidateQueries({ queryKey: [key, "content"] }); qc.invalidateQueries({ queryKey: [key, "teams"] }); onClose(); };
  const payload = () => ({ ...form, scheduled_for: form.scheduled_for ? new Date(form.scheduled_for).toISOString() : null, media: (form.media ?? []).map((m: FileRef) => m.file_id) });
  const open = creating || !!item;
  const picker = useRef<HTMLInputElement>(null);
  const channels = useQuery({ queryKey: [key, "content", "publishing"], enabled: open, queryFn: () => api<{ items: Channel[] }>("/content/publishing") });
  const files = useQuery({ queryKey: [key, "files", "media-picker"], enabled: open && !!editable && can("files.read"), queryFn: () => api<{ items: any[] }>("/files", { params: { limit: 100 } }) });
  const media: FileRef[] = form.media ?? [];
  const addMedia = (f: any) => setForm((cur: any) => ({ ...cur, media: [...(cur.media ?? []), { file_id: f.file_id ?? f.id, name: f.name, content_type: f.content_type, size: f.size }].slice(0, 4) }));
  const upload = useMutation({ mutationFn: (file: File) => apiUpload("/files", file), onSuccess: (f) => { addMedia(f); qc.invalidateQueries({ queryKey: [key, "files"] }); } });
  const publish = useMutation({ mutationFn: () => api<Item>(`/content/${item.id}/publish`, { method: "POST" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [key, "content"] }); qc.invalidateQueries({ queryKey: [key, "teams"] }); } });
  const target = channels.data?.items.find((c) => c.channel === (item?.channel ?? form.channel));
  const available = (files.data?.items ?? []).filter((f) => isMedia(f.content_type) && !media.some((m) => m.file_id === f.id));
  const save = useMutation({ mutationFn: () => creating ? api("/content", { method: "POST", body: payload() }) : api(`/content/${item.id}`, { method: "PATCH", body: payload() }), onSuccess: done });
  const decide = useMutation({ mutationFn: (approve: boolean) => api(`/content/${item.id}/${approve ? "approve" : "reject"}`, { method: "POST", body: { note } }), onSuccess: done });
  const outcome = useMutation({ mutationFn: (status: string) => api(`/content/${item.id}/outcome`, { method: "POST", body: { status, url: url || null, note } }), onSuccess: done });
  const remove = useMutation({ mutationFn: () => api(`/content/${item.id}`, { method: "DELETE" }), onSuccess: done });
  const canDecide = item && ["awaiting_approval", "draft", "idea"].includes(item.status) && can("content.approve");
  const canWrite = can("content.write");
  const error = save.error ?? decide.error ?? outcome.error ?? remove.error ?? upload.error ?? publish.error;
  const result = publish.data;
  const canPublish = item && ["approved", "failed"].includes(item.status) && !!target?.integration_id && can("content.approve") && !result;
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
      {result?.status === "published" && <Notice tone="success">Published{result.published_url && <> - <a className="underline" href={result.published_url} target="_blank" rel="noreferrer noopener">open the post</a></>}.</Notice>}
      {result?.status === "failed" && <Notice tone="warning">{result.decision_note}</Notice>}
      {!result && item?.status === "approved" && (target?.integration_id
        ? <Notice tone="info">Approved. It will be published to {target.integration_name} automatically {item.scheduled_for && new Date(item.scheduled_for) > new Date() ? fmtRelative(item.scheduled_for) : "within a few seconds"}.</Notice>
        : <Notice tone="info">{item.scheduled_for && new Date(item.scheduled_for) > new Date() ? `Approved. It goes to the publishing workflow ${fmtRelative(item.scheduled_for)}.` : "Approved and handed to the publishing workflow."} After it is live, record the outcome below.{target?.native && " To publish automatically, connect an account on the Publishing tab."}</Notice>)}
      {!result && item?.status === "published" && <Notice tone="success">Published {fmtRelative(item.published_at)}{item.published_url && <> - <a className="underline" href={item.published_url} target="_blank" rel="noreferrer noopener">open</a></>}.</Notice>}
      {item?.decision_note && <p className="text-xs text-muted">Note: {item.decision_note}</p>}
      {editable && canWrite ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="sm:col-span-2"><Field label="Title" required><Input value={form.title ?? ""} onChange={(e) => setForm({ ...form, title: e.target.value })} /></Field></div>
          <Field label="Channel"><Select value={form.channel} onChange={(e) => setForm({ ...form, channel: e.target.value })}>{CHANNELS.map((c) => <option key={c} value={c}>{CHANNEL_LABEL[c]}</option>)}</Select></Field>
          <Field label="Publish at" hint="Your local time"><Input type="datetime-local" value={form.scheduled_for ?? ""} onChange={(e) => setForm({ ...form, scheduled_for: e.target.value })} /></Field>
          <div className="sm:col-span-2"><Field label="Text"><Textarea className="min-h-[160px]" value={form.body ?? ""} onChange={(e) => setForm({ ...form, body: e.target.value })} /></Field></div>
          <div className="sm:col-span-2"><Field label="Media brief" hint="What image or video goes with it"><Textarea value={form.media_notes ?? ""} onChange={(e) => setForm({ ...form, media_notes: e.target.value })} /></Field></div>
          <div className="sm:col-span-2" role="group" aria-label="Media">
            <div className="mb-1 text-xs font-medium">Media <span className="font-normal text-muted">- up to 4 images or videos posted with the text</span></div>
            <div className="flex flex-wrap items-start gap-2">
              {media.map((m) => (
                <div key={m.file_id} className="relative">
                  <MediaPreview file={m} controls={false} className="h-20 w-20" />
                  <button type="button" aria-label={`Remove ${m.name}`} onClick={() => setForm({ ...form, media: media.filter((x) => x.file_id !== m.file_id) })}
                    className="absolute -right-1.5 -top-1.5 rounded-full border bg-surface p-0.5 text-muted hover:text-fg"><X className="h-3 w-3" /></button>
                </div>))}
              {media.length < 4 && (
                <div className="flex min-w-[220px] flex-1 flex-col gap-1.5">
                  {available.length > 0 && <Select aria-label="Add media from files" value="" onChange={(e) => { const f = available.find((x) => x.id === e.target.value); if (f) addMedia(f); }}>
                    <option value="">Add from Files or Creative Studio...</option>{available.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}</Select>}
                  <div className="flex flex-wrap items-center gap-2">
                    <input ref={picker} type="file" accept="image/*,video/*" className="sr-only" aria-label="Upload media" onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
                    <Button size="sm" loading={upload.isPending} onClick={() => picker.current?.click()}><ImagePlus className="h-3.5 w-3.5" /> Upload</Button>
                    <Link href="/studio" className="text-xs text-primary hover:underline">Create with AI in Creative Studio</Link>
                  </div>
                </div>)}
            </div>
          </div>
        </div>
      ) : item && (
        <div className="space-y-3">
          <div className="whitespace-pre-wrap rounded-md border bg-bg p-3 text-sm">{item.body || <span className="text-muted">No text</span>}</div>
          {item.media?.length > 0 && <div className="flex flex-wrap gap-2" role="group" aria-label="Media">{item.media.map((m: FileRef) => <MediaPreview key={m.file_id} file={m} className="h-28 w-28" />)}</div>}
          {item.media_notes && <div><div className="text-2xs font-medium uppercase tracking-wider text-muted">Media brief</div><p className="text-sm">{item.media_notes}</p></div>}
        </div>
      )}
      {item?.hashtags?.length > 0 && <div className="flex flex-wrap gap-1">{item.hashtags.map((h: string) => <Badge key={h}>#{h}</Badge>)}</div>}
      {canDecide && <Field label="Note (optional)" hint="Shown with the decision; agents read it when they plan again."><Input value={note} onChange={(e) => setNote(e.target.value)} /></Field>}
      {canPublish && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-primary/40 p-3">
          <span className="text-sm">Publish to <span className="font-medium">{target!.integration_name}</span>{item.status === "failed" ? " again" : " now instead of waiting"}.</span>
          <Button variant="primary" loading={publish.isPending} onClick={() => publish.mutate()}><Send className="h-3.5 w-3.5" /> {item.status === "failed" ? "Publish again" : "Publish now"}</Button>
        </div>
      )}
      {item && ["approved", "failed"].includes(item.status) && canWrite && !result && (
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

function PublishingPanel() {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { can } = useSession();
  const channels = useQuery({ queryKey: [key, "content", "publishing"], queryFn: () => api<{ items: Channel[] }>("/content/publishing") });
  const integrations = useQuery({ queryKey: [key, "integrations", "for-publishing"], enabled: can("integrations.read"), queryFn: () => api<{ items: any[] }>("/integrations") });
  const connect = useMutation({ mutationFn: (v: { channel: string; integration_id: string | null }) => api("/content/publishing", { method: "PUT", body: v }),
    onSuccess: () => qc.invalidateQueries({ queryKey: [key, "content", "publishing"] }) });
  const manage = can("content.approve");
  if (channels.isLoading) return <Card><Spinner /></Card>;
  if (channels.error) return <Card><div className="p-4"><ErrorState error={channels.error} /></div></Card>;
  const native = channels.data!.items.filter((c) => c.native);
  const manual = channels.data!.items.filter((c) => !c.native);
  return (
    <Card>
      <div className="border-b px-4 py-3">
        <h2 className="text-sm font-semibold">Where approved content is published</h2>
        <p className="mt-0.5 text-xs text-muted">Connect a channel to one of your accounts and approved items are posted there automatically at their scheduled time. Nothing is ever posted without approval.</p>
      </div>
      <InlineError error={connect.error} />
      <ul className="divide-y divide-line/60">{native.map((c) => {
        const options = (integrations.data?.items ?? []).filter((i) => c.providers.includes(i.provider_key));
        return (
          <li key={c.channel} className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-3">
            <span className="w-28 text-sm font-medium">{CHANNEL_LABEL[c.channel] ?? c.channel}{c.channel === "other" && <span className="block text-2xs font-normal text-muted">Telegram channel</span>}</span>
            {c.integration_id ? <Badge tone={c.integration_status === "connected" ? "success" : "danger"}>{c.integration_status === "connected" ? "Publishes automatically" : `Account ${label(c.integration_status ?? "error")}`}</Badge> : <Badge>Handed to a workflow</Badge>}
            <div className="ml-auto w-64 max-w-full">
              {manage ? (
                <Select aria-label={`Account for ${CHANNEL_LABEL[c.channel] ?? c.channel}`} value={c.integration_id ?? ""} disabled={connect.isPending}
                  onChange={(e) => connect.mutate({ channel: c.channel, integration_id: e.target.value || null })}>
                  <option value="">Not connected</option>
                  {options.map((i) => <option key={i.id} value={i.id}>{i.name}{i.status !== "connected" ? ` (${label(i.status)})` : ""}</option>)}
                </Select>
              ) : <span className="text-sm text-muted">{c.integration_name ?? "Not connected"}</span>}
              {manage && options.length === 0 && <Link href="/integrations" className="mt-1 block text-xs text-primary hover:underline">Connect {c.providers.map((p) => p === "meta" ? "Meta" : p === "x" ? "X" : p[0].toUpperCase() + p.slice(1)).join(" or ")} under Integrations</Link>}
            </div>
          </li>);
      })}</ul>
      <p className="border-t px-4 py-3 text-xs text-muted">{manual.map((c) => CHANNEL_LABEL[c.channel] ?? c.channel).join(", ")}: no direct publishing yet. Approved items start your publishing workflow (the Content Due event), or you post them by hand and mark them published.</p>
    </Card>
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
      <Tabs value={tab} onChange={setTab} tabs={[{ key: "calendar", label: "Calendar" }, { key: "approval", label: "Awaiting approval", count: awaiting.data?.items.length }, { key: "all", label: "Recently changed" }, { key: "publishing", label: "Publishing" }]} />
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
        {tab === "publishing" && <PublishingPanel />}
        {tab !== "calendar" && tab !== "publishing" && (() => {
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
