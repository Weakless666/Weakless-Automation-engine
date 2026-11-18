"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Clapperboard, Download, ImageIcon, Send, Sparkles, Trash2 } from "lucide-react";
import clsx from "clsx";
import { api, apiDownload, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtRelative } from "@/lib/format";
import { MediaPreview, type FileRef } from "@/components/media-preview";
import { Badge, Button, Card, Dialog, EmptyState, ErrorState, Field, InlineError, Input, Notice, PageHeader, Pager, Select, Spinner, Textarea } from "@/components/ui";

const LIMIT = 24;
const ASPECT_LABEL: Record<string, string> = { "1:1": "Square (1:1)", "4:5": "Portrait feed (4:5)", "16:9": "Landscape (16:9)", "9:16": "Vertical story / reel (9:16)" };
const ASPECT_BOX: Record<string, string> = { "1:1": "aspect-square", "4:5": "aspect-[4/5]", "16:9": "aspect-video", "9:16": "aspect-[9/16]" };
const CHANNELS: [string, string][] = [["instagram", "Instagram"], ["facebook", "Facebook"], ["linkedin", "LinkedIn"], ["x", "X"], ["tiktok", "TikTok"], ["youtube", "YouTube"],
  ["blog", "Blog"], ["newsletter", "Newsletter"], ["email", "Email"], ["other", "Other"]];

type Provider = { key: string; name: string; available: boolean; image_models: string[]; video_models: string[] };
type Meta = { providers: Provider[]; aspects: string[]; video_seconds: number[] };
type Generation = { id: string; kind: "image" | "video"; provider: string; model: string; prompt: string; params: { aspect?: string; seconds?: number };
  status: "queued" | "running" | "completed" | "failed"; error: string; file: FileRef | null; created_at: string };

function UseInPost({ generation, onClose }: { generation: Generation | null; onClose: () => void }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [channel, setChannel] = useState("instagram");
  useEffect(() => { if (generation) setTitle(generation.prompt.slice(0, 80)); }, [generation]);
  const create = useMutation({
    mutationFn: () => api("/content", { method: "POST", body: { title, channel, status: "draft", media: [generation!.file!.file_id], media_notes: generation!.prompt.slice(0, 5000) } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: [key, "content"] }); onClose(); router.push("/content"); },
  });
  return (
    <Dialog open={!!generation} onClose={onClose} title="Use in a post" description="Creates a draft in the content calendar with this file attached. You write the text and approve it there."
      footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!title.trim()} onClick={() => create.mutate()}>Create draft</Button></>}>
      <Field label="Title" required><Input value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
      <Field label="Channel"><Select value={channel} onChange={(e) => setChannel(e.target.value)}>{CHANNELS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select></Field>
      <InlineError error={create.error} />
    </Dialog>
  );
}

export default function StudioPage() {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { org, can } = useSession();
  const [kind, setKind] = useState<"image" | "video">("image");
  const [prompt, setPrompt] = useState("");
  const [aspect, setAspect] = useState("1:1");
  const [seconds, setSeconds] = useState(8);
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [offset, setOffset] = useState(0);
  const [posting, setPosting] = useState<Generation | null>(null);
  const write = can("content.write");
  const meta = useQuery({ queryKey: [key, "studio", "meta"], enabled: !!org, queryFn: () => api<Meta>("/studio/meta") });
  const list = useQuery({ queryKey: [key, "studio", "generations", offset], enabled: !!org,
    queryFn: () => api<Page<Generation>>("/studio/generations", { params: { limit: LIMIT, offset } }),
    // Follow work in progress; stay quiet otherwise.
    refetchInterval: (q) => (q.state.data?.items.some((g) => g.status === "queued" || g.status === "running") ? 3000 : false) });
  const refresh = () => qc.invalidateQueries({ queryKey: [key, "studio", "generations"] });
  const generate = useMutation({
    mutationFn: () => api("/studio/generations", { method: "POST", body: { kind, prompt, aspect, seconds, provider: provider || null, model: model.trim() || null } }),
    onSuccess: () => { setOffset(0); refresh(); },
  });
  const remove = useMutation({ mutationFn: (id: string) => api(`/studio/generations/${id}`, { method: "DELETE" }), onSuccess: () => { refresh(); qc.invalidateQueries({ queryKey: [key, "files"] }); } });
  const download = useMutation({ mutationFn: (f: FileRef) => apiDownload(`/files/${f.file_id}/content`, f.name) });

  if (!org) return <><PageHeader title="Creative Studio" /><Notice tone="warning">Select an organization to open its studio.</Notice></>;
  const providers = (meta.data?.providers ?? []).filter((p) => p.available);
  const forKind = providers.filter((p) => (kind === "image" ? p.image_models.length > 0 || p.key === "openai_compatible" : p.video_models.length > 0));
  const chosen = forKind.find((p) => p.key === provider) ?? forKind[0];
  const suggestions = chosen ? (kind === "image" ? chosen.image_models : chosen.video_models) : [];
  const aspects = (meta.data?.aspects ?? Object.keys(ASPECT_LABEL)).filter((a) => kind === "image" || a === "16:9" || a === "9:16");
  const ready = !!chosen && prompt.trim().length >= 3;

  return (
    <>
      <PageHeader title="Creative Studio" description="Create images and short videos with AI for your posts. Everything you make is kept in Files and can be attached to content." />
      {meta.data && providers.length === 0 && (
        <Notice tone="warning">No AI provider that makes images or videos is connected. Connect OpenAI or Google Gemini under <Link href="/integrations" className="underline">Integrations</Link>, then come back.</Notice>
      )}
      {write && (
        <Card className="mb-4">
          <form className="grid gap-3 p-4 lg:grid-cols-[1fr_300px]" onSubmit={(e) => { e.preventDefault(); if (ready) generate.mutate(); }}>
            <div className="space-y-3">
              <div className="inline-flex rounded-md border p-0.5" role="group" aria-label="What to create">
                {(["image", "video"] as const).map((k) => (
                  <button key={k} type="button" aria-pressed={kind === k} onClick={() => { setKind(k); setModel(""); if (k === "video" && aspect !== "9:16") setAspect("16:9"); }}
                    className={clsx("flex items-center gap-1.5 rounded px-3 py-1.5 text-sm", kind === k ? "bg-primary text-primary-fg" : "text-muted hover:text-fg")}>
                    {k === "image" ? <ImageIcon className="h-3.5 w-3.5" /> : <Clapperboard className="h-3.5 w-3.5" />}{k === "image" ? "Image" : "Video"}
                  </button>))}
              </div>
              <Field label="Describe it" hint={kind === "image" ? "Subject, setting, style, colours, mood. Say if it should leave space for text." : "What happens, the setting, camera movement and mood. Videos take a few minutes."}>
                <Textarea className="min-h-[120px]" value={prompt} maxLength={8000} onChange={(e) => setPrompt(e.target.value)}
                  placeholder={kind === "image" ? "A bright yoga studio at sunrise, warm light, a single mat on a wooden floor, calm and minimal" : "Slow pan across a bright yoga studio at sunrise, soft light, calm"} />
              </Field>
            </div>
            <div className="space-y-3">
              <Field label="Format"><Select value={aspect} onChange={(e) => setAspect(e.target.value)}>{aspects.map((a) => <option key={a} value={a}>{ASPECT_LABEL[a] ?? a}</option>)}</Select></Field>
              {kind === "video" && <Field label="Length"><Select value={seconds} onChange={(e) => setSeconds(Number(e.target.value))}>{(meta.data?.video_seconds ?? [4, 8, 12]).map((s) => <option key={s} value={s}>{s} seconds</option>)}</Select></Field>}
              <Field label="AI provider"><Select value={chosen?.key ?? ""} onChange={(e) => { setProvider(e.target.value); setModel(""); }} disabled={forKind.length === 0}>
                {forKind.length === 0 && <option value="">None connected for {kind}s</option>}{forKind.map((p) => <option key={p.key} value={p.key}>{p.name}</option>)}</Select></Field>
              <Field label="Model" hint={suggestions.length ? `Default: ${suggestions[0]}` : "Enter the model id your server offers"}>
                <Input value={model} onChange={(e) => setModel(e.target.value)} list="studio-models" placeholder={suggestions[0] ?? "model id"} />
                <datalist id="studio-models">{suggestions.map((m) => <option key={m} value={m} />)}</datalist>
              </Field>
              <Button type="submit" variant="primary" className="w-full" loading={generate.isPending} disabled={!ready}><Sparkles className="h-3.5 w-3.5" /> Generate {kind}</Button>
            </div>
          </form>
          <div className="px-4 pb-3"><InlineError error={generate.error} /></div>
        </Card>
      )}
      <InlineError error={remove.error ?? download.error} />
      {list.isLoading ? <Card><Spinner /></Card> : list.error ? <Card><div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div></Card> : list.data!.items.length === 0 ? (
        <Card><EmptyState icon={<Sparkles className="h-5 w-5" />} title="Nothing created yet" description="Describe an image or a video above. Results appear here and in Files." /></Card>
      ) : (
        <>
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4" aria-label="Generations">{list.data!.items.map((g) => (
            <li key={g.id}>
              <Card className="flex h-full flex-col overflow-hidden">
                <div className={clsx("w-full bg-raised", ASPECT_BOX[g.params.aspect ?? "1:1"] ?? "aspect-square", "max-h-80")}>
                  {g.status === "completed" && g.file ? <MediaPreview file={g.file} className="h-full w-full rounded-none" />
                    : g.status === "failed" ? <div className="flex h-full items-center justify-center p-4 text-center text-xs text-danger" role="alert">{g.error || "This request failed."}</div>
                    : <div className="flex h-full flex-col items-center justify-center gap-2 text-xs text-muted" aria-live="polite"><Spinner />{g.kind === "video" ? "Rendering the video. This takes a few minutes." : "Creating the image..."}</div>}
                </div>
                <div className="flex flex-1 flex-col gap-2 p-3">
                  <p className="line-clamp-3 text-xs" title={g.prompt}>{g.prompt}</p>
                  <div className="flex flex-wrap items-center gap-1.5 text-2xs text-muted">
                    <Badge tone={g.status === "completed" ? "success" : g.status === "failed" ? "danger" : "info"}>{g.status}</Badge>
                    <span>{g.kind}{g.params.seconds ? `, ${g.params.seconds}s` : ""}</span><span>{g.model}</span><span>{fmtRelative(g.created_at)}</span>
                  </div>
                  <div className="mt-auto flex flex-wrap justify-end gap-1.5">
                    {g.file && write && <Button size="sm" onClick={() => setPosting(g)}><Send className="h-3.5 w-3.5" /> Use in a post</Button>}
                    {g.file && <Button size="sm" variant="ghost" aria-label={`Download ${g.file.name}`} loading={download.isPending && download.variables?.file_id === g.file.file_id} onClick={() => download.mutate(g.file!)}><Download className="h-3.5 w-3.5" /></Button>}
                    {write && g.status !== "queued" && g.status !== "running" && <Button size="sm" variant="ghost" aria-label={`Delete "${g.prompt.slice(0, 40)}"`}
                      onClick={() => window.confirm(g.file ? "Delete this and its file?" : "Delete this request?") && remove.mutate(g.id)}><Trash2 className="h-3.5 w-3.5" /></Button>}
                  </div>
                </div>
              </Card>
            </li>))}</ul>
          <div className="mt-3"><Pager total={list.data!.total} limit={LIMIT} offset={offset} onChange={setOffset} /></div>
        </>
      )}
      <UseInPost generation={posting} onClose={() => setPosting(null)} />
    </>
  );
}
