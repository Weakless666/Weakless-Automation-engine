"use client";

import Link from "next/link";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Download, FolderOpen, Trash2, Upload } from "lucide-react";
import clsx from "clsx";
import { api, apiDownload, apiUpload, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtDate, fmtRelative } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorState, InlineError, Input, Notice, PageHeader, Pager, Select, Spinner, Table, Td, Th } from "@/components/ui";

const LIMIT = 50;
const SOURCE: Record<string, string> = { upload: "Uploaded", workflow: "Workflow", mailbox: "Email attachment", studio: "Creative Studio" };

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

export default function FilesPage() {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { org, can } = useSession();
  const [offset, setOffset] = useState(0);
  const [source, setSource] = useState("");
  const [q, setQ] = useState("");
  const [over, setOver] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const picker = useRef<HTMLInputElement>(null);
  const list = useQuery({ queryKey: [key, "files", offset, source, q], enabled: !!org,
    queryFn: () => api<Page<any> & { storage: { used_bytes: number; limit_bytes: number; max_file_bytes: number } }>("/files", { params: { limit: LIMIT, offset, source, q } }) });
  const refresh = () => qc.invalidateQueries({ queryKey: [key, "files"] });
  const upload = useMutation({ mutationFn: async (files: File[]) => { for (const f of files) await apiUpload("/files", f); }, onSettled: refresh });
  const remove = useMutation({ mutationFn: (id: string) => api(`/files/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const download = useMutation({ mutationFn: (f: any) => apiDownload(`/files/${f.id}/content`, f.name) });
  const write = can("files.write");
  const add = (files: FileList | null) => { if (files?.length) upload.mutate(Array.from(files)); };

  if (!org) return <><PageHeader title="Files" /><Notice tone="warning">Select an organization to see its files.</Notice></>;
  const storage = list.data?.storage;
  const pct = storage ? Math.min(100, (storage.used_bytes / Math.max(1, storage.limit_bytes)) * 100) : 0;
  return (
    <>
      <PageHeader title="Files" description="Documents, images and exports that workflows use or produce: uploads, downloads, email attachments and generated files."
        actions={write && <>
          <input ref={picker} type="file" multiple className="sr-only" aria-label="Choose files to upload" onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
          <Button variant="primary" loading={upload.isPending} onClick={() => picker.current?.click()}><Upload className="h-3.5 w-3.5" /> Upload files</Button>
        </>} />
      {storage && (
        <div className="mb-4 flex flex-wrap items-center gap-3 text-xs text-muted">
          <div className="h-1.5 w-40 overflow-hidden rounded-full bg-raised" role="progressbar" aria-label="Storage used" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
            <div className={clsx("h-full rounded-full", pct > 90 ? "bg-danger" : "bg-primary")} style={{ width: `${pct}%` }} />
          </div>
          <span>{fmtBytes(storage.used_bytes)} of {fmtBytes(storage.limit_bytes)} used. Up to {fmtBytes(storage.max_file_bytes)} per file.</span>
        </div>
      )}
      <InlineError error={upload.error ?? remove.error ?? download.error} />
      <Card
        onDragOver={write ? (e) => { e.preventDefault(); setOver(true); } : undefined}
        onDragLeave={() => setOver(false)}
        onDrop={write ? (e) => { e.preventDefault(); setOver(false); add(e.dataTransfer.files); } : undefined}
        className={clsx(over && "border-primary ring-1 ring-primary")}>
        <div className="flex flex-wrap gap-2 border-b p-3">
          <div className="w-56"><Input value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} placeholder="Search by name..." aria-label="Search files" className="h-8 text-xs" /></div>
          <div className="w-44"><Select value={source} onChange={(e) => { setSource(e.target.value); setOffset(0); }} aria-label="Source" className="h-8 text-xs">
            <option value="">All sources</option>{Object.entries(SOURCE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select></div>
          {write && <span className="ml-auto self-center text-xs text-muted max-sm:hidden">Drop files here to upload</span>}
        </div>
        {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
          <EmptyState icon={<FolderOpen className="h-5 w-5" />} title={q || source ? "No files match" : "No files yet"}
            description="Upload a file to use it in a workflow, or let a workflow save downloads and email attachments here." />
        ) : (
          <Table>
            <thead><tr><Th>Name</Th><Th>Size</Th><Th>Source</Th><Th>Added</Th><Th>Kept until</Th><Th /></tr></thead>
            <tbody>{list.data!.items.map((f) => (
              <tr key={f.id} className="hover:bg-raised/50">
                <Td><div className="max-w-[320px] truncate font-medium" title={f.name}>{f.name}</div><div className="font-mono text-2xs text-muted">{f.content_type}</div></Td>
                <Td className="tabular whitespace-nowrap">{fmtBytes(f.size)}</Td>
                <Td><Badge>{SOURCE[f.source] ?? f.source}</Badge>{f.execution_id && <Link href={`/executions/${f.execution_id}`} className="ml-2 text-xs text-primary hover:underline">run</Link>}</Td>
                <Td className="whitespace-nowrap text-muted">{fmtRelative(f.created_at)}</Td>
                <Td className="whitespace-nowrap text-muted">{f.expires_at ? fmtDate(f.expires_at) : "Until deleted"}</Td>
                <Td className="text-right"><div className="flex justify-end gap-1.5">
                  <Button size="sm" variant="ghost" title="Copy the id to use this file in a workflow step" onClick={async () => { try { await navigator.clipboard.writeText(f.id); setCopied(f.id); setTimeout(() => setCopied(null), 1500); } catch { /* clipboard blocked */ } }}>{copied === f.id ? "Copied" : "Copy ID"}</Button>
                  <Button size="sm" aria-label={`Download ${f.name}`} loading={download.isPending && download.variables?.id === f.id} onClick={() => download.mutate(f)}><Download className="h-3.5 w-3.5" /></Button>
                  {write && <Button size="sm" variant="ghost" aria-label={`Delete ${f.name}`} onClick={() => window.confirm(`Delete ${f.name}? Workflows that use it will fail at that step.`) && remove.mutate(f.id)}><Trash2 className="h-3.5 w-3.5" /></Button>}
                </div></Td>
              </tr>))}</tbody>
          </Table>
        )}
        {list.data && <div className="p-3"><Pager total={list.data.total} limit={LIMIT} offset={offset} onChange={setOffset} /></div>}
      </Card>
    </>
  );
}
