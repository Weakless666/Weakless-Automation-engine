"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Package } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { titleCase } from "@/lib/format";
import { Badge, Button, Card, Checkbox, Dialog, EmptyState, ErrorState, Field, InlineError, Input, Notice, PageHeader, Spinner } from "@/components/ui";

function PackageDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { org, can, isStaff } = useSession();
  const [selected, setSelected] = useState<string[]>([]);
  const [businessName, setBusinessName] = useState("");
  const [result, setResult] = useState<any[] | null>(null);
  const pkg = useQuery({ queryKey: ["package", id], queryFn: () => api(`/industry-packages/${id}`, { org: false }), enabled: !!id });
  const installed = useQuery({ queryKey: [key, "installed"], queryFn: () => api<Page<any>>("/installed-templates", { params: { limit: 200 } }), enabled: !!id && !!org });
  const have = new Set((installed.data?.items ?? []).map((i) => i.template_id));
  useEffect(() => { if (pkg.data) { setSelected(pkg.data.templates.filter((t: any) => !have.has(t.id)).map((t: any) => t.id)); setResult(null); } }, [pkg.data, installed.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const allowed = !!org && can("templates.install") && (isStaff || org.mode === "saas");
  const install = useMutation({
    mutationFn: () => api<{ installed: any[] }>(`/industry-packages/${id}/install`, { method: "POST", body: { template_ids: selected, config: businessName ? { business_name: businessName } : {} } }),
    onSuccess: (r) => { setResult(r.installed); qc.invalidateQueries({ queryKey: [key] }); },
  });
  return (
    <Dialog open={!!id} onClose={onClose} wide title={pkg.data?.name ?? "Industry package"} description={pkg.data?.description}
      footer={<><Button onClick={onClose}>{result ? "Done" : "Close"}</Button>{allowed && !result && <Button variant="primary" loading={install.isPending} disabled={selected.length === 0} onClick={() => install.mutate()}>Install {selected.length} {selected.length === 1 ? "automation" : "automations"}</Button>}</>}>
      {pkg.isLoading ? <Spinner /> : pkg.error ? <ErrorState error={pkg.error} /> : pkg.data && (result ? (
        <>
          <Notice tone="success">{result.length} {result.length === 1 ? "automation was" : "automations were"} installed as draft workflows. Finish their configuration, then test, publish and activate each one.</Notice>
          <ul className="divide-y divide-line/60 rounded-md border">{result.map((i) => (
            <li key={i.id} className="flex items-center justify-between px-3 py-2 text-sm"><span>{i.template_name}{i.missing_config.length > 0 && <span className="ml-2 text-xs text-warning">needs: {i.missing_config.join(", ")}</span>}</span>
              {can("workflows.write") && <Link href={`/builder/${i.workflow_id}`} className="text-xs text-primary hover:underline">Open in builder</Link>}</li>))}</ul>
          <Link href="/templates" className="text-xs text-primary hover:underline">Configure installed templates</Link>
        </>
      ) : (
        <>
          {!allowed && <Notice tone="warning">{!org ? "Select an organization to install this package." : "Packages for this organization are installed by your Weakless team."}</Notice>}
          <ul className="divide-y divide-line/60 rounded-md border">{pkg.data.templates.map((t: any) => (
            <li key={t.id} className="flex items-start gap-3 px-3 py-2.5">
              {allowed && <div className="pt-0.5"><Checkbox label={<span className="sr-only">{t.name}</span>} checked={selected.includes(t.id)} disabled={have.has(t.id)}
                onChange={(on) => setSelected(on ? [...selected, t.id] : selected.filter((x) => x !== t.id))} /></div>}
              <div className="min-w-0 flex-1"><div className="text-sm font-medium">{t.name} {have.has(t.id) && <Badge tone="success" className="ml-1">Installed</Badge>}</div>
                <div className="text-xs text-muted">{t.description}</div>
                <div className="mt-1 flex flex-wrap gap-1">{t.required_integrations.map((r: string) => <Badge key={r}>{r}</Badge>)}</div></div>
            </li>))}</ul>
          {allowed && <Field label="Business name" hint="Applied to every automation in the package that uses it. Other settings are configured per automation afterwards.">
            <Input value={businessName} onChange={(e) => setBusinessName(e.target.value)} placeholder={org?.name} /></Field>}
          <InlineError error={install.error} />
        </>
      ))}
    </Dialog>
  );
}

export default function SolutionsPage() {
  const [open, setOpen] = useState<string | null>(null);
  const list = useQuery({ queryKey: ["packages"], queryFn: () => api<{ items: any[] }>("/industry-packages", { org: false }) });
  return (
    <>
      <PageHeader title="Industry Solutions" description="Curated collections of templates for a specific type of business. Install the whole package or only the automations you need." />
      {list.isLoading ? <Spinner /> : list.error ? <ErrorState error={list.error} onRetry={() => list.refetch()} /> : list.data!.items.length === 0 ? (
        <Card><EmptyState icon={<Package className="h-5 w-5" />} title="No packages published" /></Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {list.data!.items.map((p) => (
            <Card key={p.id} className="flex flex-col p-5">
              <div className="flex items-start justify-between gap-3"><h2 className="text-base font-semibold">{p.name}</h2><Badge tone="primary">{titleCase(p.industry)}</Badge></div>
              <p className="mt-1 text-sm text-muted">{p.description}</p>
              <ul className="mt-3 grid flex-1 grid-cols-2 gap-x-4 gap-y-1 text-xs text-fg/85">{p.template_names.map((n: string) => <li key={n} className="truncate before:mr-1.5 before:text-primary before:content-['-']">{n}</li>)}</ul>
              <div className="mt-4 flex items-center justify-between"><span className="text-xs text-muted">{p.template_count} automations</span><Button variant="primary" size="sm" onClick={() => setOpen(p.id)}>View package</Button></div>
            </Card>
          ))}
        </div>
      )}
      <PackageDialog id={open} onClose={() => setOpen(null)} />
    </>
  );
}
