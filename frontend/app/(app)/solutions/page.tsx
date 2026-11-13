"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Package } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { titleCase } from "@/lib/format";
import { Badge, Button, Card, Checkbox, Dialog, EmptyState, ErrorState, Field, InlineError, Input, Notice, PageHeader, Spinner } from "@/components/ui";
import { SchemaForm } from "@/components/schema-form";

function PackageDialog({ id, onClose }: { id: string | null; onClose: () => void }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const { org, can, isStaff } = useSession();
  const [selected, setSelected] = useState<string[]>([]);
  const [businessName, setBusinessName] = useState("");
  const [teamConfig, setTeamConfig] = useState<Record<string, any>>({});
  const [result, setResult] = useState<any[] | null>(null);
  const [team, setTeam] = useState<any | null>(null);
  const pkg = useQuery({ queryKey: ["package", id], queryFn: () => api(`/industry-packages/${id}`, { org: false }), enabled: !!id });
  const installed = useQuery({ queryKey: [key, "installed"], queryFn: () => api<Page<any>>("/installed-templates", { params: { limit: 200 } }), enabled: !!id && !!org });
  const have = new Set((installed.data?.items ?? []).map((i) => i.template_id));
  // A new package opens fresh; background refreshes only update the selection and never hide a finished install.
  useEffect(() => { setResult(null); setTeam(null); setBusinessName(""); setTeamConfig({}); }, [id]);
  const teamSpec = pkg.data?.team;
  const missingTeam = teamSpec ? (teamSpec.config_schema.required as string[]).filter((k) => !String(teamConfig[k] ?? "").trim()) : [];
  useEffect(() => { if (pkg.data) setSelected(pkg.data.templates.filter((t: any) => !have.has(t.id)).map((t: any) => t.id)); }, [pkg.data, installed.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const allowed = !!org && can("templates.install") && (isStaff || org.mode === "saas");
  const install = useMutation({
    mutationFn: () => api<{ installed: any[]; team?: any }>(`/industry-packages/${id}/install`, { method: "POST",
      body: { template_ids: selected, config: teamSpec ? teamConfig : businessName ? { business_name: businessName } : {} } }),
    onSuccess: (r) => { setResult(r.installed); setTeam(r.team ?? null); qc.invalidateQueries({ queryKey: [key] }); },
  });
  const modules = teamSpec ? (teamSpec.members as any[]).reduce((acc: Record<string, any[]>, m) => ({ ...acc, [m.module]: [...(acc[m.module] ?? []), m] }), {}) : {};
  return (
    <Dialog open={!!id} onClose={onClose} wide title={pkg.data?.name ?? "Industry package"} description={pkg.data?.description}
      footer={<><Button onClick={onClose}>{result ? "Done" : "Close"}</Button>{allowed && !result && <Button variant="primary" loading={install.isPending} disabled={selected.length === 0 || missingTeam.length > 0} onClick={() => install.mutate()}>
        {teamSpec ? `Install team and ${selected.length} ${selected.length === 1 ? "automation" : "automations"}` : `Install ${selected.length} ${selected.length === 1 ? "automation" : "automations"}`}</Button>}</>}>
      {pkg.isLoading ? <Spinner /> : pkg.error ? <ErrorState error={pkg.error} /> : pkg.data && (result ? (
        <>
          {team && <Notice tone="success">{team.agents.length} AI agents are ready in the {teamSpec?.name ?? "team"}. <Link href="/teams" className="underline">Open the team</Link> or the <Link href="/content" className="underline">content calendar</Link>.</Notice>}
          <Notice tone="success">{result.length} {result.length === 1 ? "automation was" : "automations were"} installed as draft workflows. Finish their configuration, then test, publish and activate each one.</Notice>
          <ul className="divide-y divide-line/60 rounded-md border">{result.map((i) => (
            <li key={i.id} className="flex items-center justify-between px-3 py-2 text-sm"><span>{i.template_name}{i.missing_config.length > 0 && <span className="ml-2 text-xs text-warning">needs: {i.missing_config.join(", ")}</span>}</span>
              {can("workflows.write") && <Link href={`/builder/${i.workflow_id}`} className="text-xs text-primary hover:underline">Open in builder</Link>}</li>))}</ul>
          <Link href="/templates" className="text-xs text-primary hover:underline">Configure installed templates</Link>
        </>
      ) : (
        <>
          {!allowed && <Notice tone="warning">{!org ? "Select an organization to install this package." : "Packages for this organization are installed by your Weakless team."}</Notice>}
          {teamSpec && (
            <section aria-label="Team members" className="rounded-md border p-3">
              <div className="mb-2 text-sm font-medium">{teamSpec.name}: {teamSpec.members.length} AI agents</div>
              <div className="grid gap-3 sm:grid-cols-2">{Object.entries<any[]>(modules).map(([mod, ms]) => (
                <div key={mod}><div className="text-2xs font-medium uppercase tracking-wider text-muted">{mod}</div>
                  <ul className="mt-1 space-y-0.5">{ms.map((m) => <li key={m.key} className="text-xs"><span className="font-medium">{m.name}</span> <span className="text-muted">- {m.description}</span></li>)}</ul></div>))}
              </div>
              <p className="mt-2 text-xs text-muted">Agents propose; nothing is published or booked without a person's approval.</p>
            </section>
          )}
          <ul className="divide-y divide-line/60 rounded-md border">{pkg.data.templates.map((t: any) => (
            <li key={t.id} className="flex items-start gap-3 px-3 py-2.5">
              {allowed && <div className="pt-0.5"><Checkbox label={<span className="sr-only">{t.name}</span>} checked={selected.includes(t.id)} disabled={have.has(t.id)}
                onChange={(on) => setSelected(on ? [...selected, t.id] : selected.filter((x) => x !== t.id))} /></div>}
              <div className="min-w-0 flex-1"><div className="text-sm font-medium">{t.name} {have.has(t.id) && <Badge tone="success" className="ml-1">Installed</Badge>}</div>
                <div className="text-xs text-muted">{t.description}</div>
                <div className="mt-1 flex flex-wrap gap-1">{t.required_integrations.map((r: string) => <Badge key={r}>{r}</Badge>)}</div></div>
            </li>))}</ul>
          {allowed && teamSpec && (
            <section aria-label="Team brief" className="space-y-2">
              <div className="text-sm font-medium">Brief the team</div>
              <p className="text-xs text-muted">This becomes the team's playbook. You can edit it later in Knowledge bases.</p>
              <SchemaForm schema={teamSpec.config_schema} value={teamConfig} onChange={setTeamConfig} />
            </section>
          )}
          {allowed && !teamSpec && <Field label="Business name" hint="Applied to every automation in the package that uses it. Other settings are configured per automation afterwards.">
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
