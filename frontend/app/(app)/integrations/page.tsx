"use client";

import { Suspense, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plug, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtRelative } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Dialog, EmptyState, ErrorState, Field, InlineError, Input, NeedOrg, Notice, PageHeader, Spinner, StatusBadge, Table, Tabs, Td, Th } from "@/components/ui";

const AUTH_LABEL: Record<string, string> = { oauth2: "OAuth", api_key: "API key", bot_token: "Bot token", webhook_url: "Webhook URL", basic: "Credentials", connection: "Connection" };

function ConnectDialog({ provider, existing, onClose }: { provider: any | null; existing?: any; onClose: () => void }) {
  const key = useOrgKey();
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const reset = () => { setName(""); setValues({}); };
  const save = useMutation({
    mutationFn: async () => {
      const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
      const integration: any = existing
        ? await api(`/integrations/${existing.id}`, { method: "PATCH", body: { name: name || undefined, values: filled } })
        : await api("/integrations", { method: "POST", body: { provider_key: provider.key, name: name || provider.name, values: filled } });
      if (provider.auth_type === "oauth2" && integration.status !== "connected") {
        const { authorization_url } = await api<{ authorization_url: string }>(`/integrations/${integration.id}/oauth/start`, { method: "POST" });
        window.location.href = authorization_url;  // continue at the provider; we return through the OAuth callback
        return integration;
      }
      return integration;
    },
    onSuccess: (i: any) => { qc.invalidateQueries({ queryKey: [key, "integrations"] }); if (!(provider.auth_type === "oauth2" && i.status !== "connected")) { reset(); onClose(); } },
    onError: () => qc.invalidateQueries({ queryKey: [key, "integrations"] }),
  });
  if (!provider) return null;
  const missing = !existing && provider.fields.some((f: any) => f.required && !values[f.name]);
  return (
    <Dialog open onClose={() => { reset(); onClose(); }} title={`${existing ? "Update" : "Connect"} ${provider.name}`}
      description={provider.auth_type === "oauth2" ? "You will be redirected to the provider to grant access." : "Credentials are encrypted before they are stored and are never shown again."}
      footer={<><Button onClick={() => { reset(); onClose(); }}>Cancel</Button>
        <Button variant="primary" loading={save.isPending} disabled={missing || !provider.available} onClick={() => save.mutate()}>{provider.auth_type === "oauth2" ? "Continue to provider" : existing ? "Save and test" : "Connect and test"}</Button></>}>
      {!provider.available && <Notice tone="warning">{provider.unavailable_reason}. Ask a platform administrator to configure it.</Notice>}
      {provider.setup && <Notice>{provider.setup}</Notice>}
      <Field label="Connection name" hint="Helps you tell several accounts of the same provider apart."><Input value={name} onChange={(e) => setName(e.target.value)} placeholder={existing?.name ?? provider.name} /></Field>
      {provider.fields.map((f: any) => (
        <Field key={f.name} label={f.label} required={f.required && !existing} hint={existing && f.secret ? "Leave empty to keep the stored value." : f.help}>
          <Input type={f.secret ? "password" : "text"} autoComplete="off" placeholder={existing && !f.secret ? existing.config?.[f.name] ?? f.placeholder : f.placeholder}
            value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />
        </Field>
      ))}
      {provider.scopes.length > 0 && <p className="text-xs text-muted">Requested permissions: <span className="font-mono">{provider.scopes.join(", ")}</span></p>}
      <InlineError error={save.error} />
    </Dialog>
  );
}

function IntegrationsInner() {
  const { org, can } = useSession();
  const key = useOrgKey();
  const qc = useQueryClient();
  const oauth = useSearchParams().get("oauth");
  const [tab, setTab] = useState<"connected" | "catalog">("connected");
  const [category, setCategory] = useState("");
  const [connecting, setConnecting] = useState<{ provider: any; existing?: any } | null>(null);
  const [tested, setTested] = useState<Record<string, string>>({});
  const providers = useQuery({ queryKey: [key, "providers"], queryFn: () => api<{ items: any[] }>("/integrations/providers"), enabled: !!org, staleTime: 300_000 });
  const list = useQuery({ queryKey: [key, "integrations"], queryFn: () => api<{ items: any[] }>("/integrations"), enabled: !!org });
  const refresh = () => qc.invalidateQueries({ queryKey: [key, "integrations"] });
  const test = useMutation({ mutationFn: (id: string) => api(`/integrations/${id}/test`, { method: "POST" }),
    onSuccess: (r: any, id) => { setTested((t) => ({ ...t, [id]: r.result.error ? `Failed: ${r.result.error}` : `OK${r.result.account ? ` - ${r.result.account}` : ""}` })); refresh(); } });
  const revoke = useMutation({ mutationFn: (id: string) => api(`/integrations/${id}/revoke`, { method: "POST" }), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => api(`/integrations/${id}`, { method: "DELETE" }), onSuccess: refresh });
  const byKey = useMemo(() => Object.fromEntries((providers.data?.items ?? []).map((p) => [p.key, p])), [providers.data]);
  const categories = useMemo(() => Array.from(new Set((providers.data?.items ?? []).map((p) => p.category))), [providers.data]);

  if (!org) return <><PageHeader title="Integrations" /><NeedOrg what="Integrations" /></>;
  const manage = can("integrations.manage");
  return (
    <>
      <PageHeader title="Integrations" description="Connections to external services, owned by this organization. An integration is shown as connected only after a live check against the provider succeeded." />
      {oauth && <div className="mb-4"><Notice tone={oauth === "connected" ? "success" : "warning"}>{oauth === "connected" ? "Authorization completed and the connection was verified." : oauth === "denied" ? "Authorization was cancelled at the provider." : "Authorization could not be completed. Check the integration's error and try again."}</Notice></div>}
      <Tabs tabs={[{ key: "connected", label: "Connections", count: list.data?.items.length }, { key: "catalog", label: "Catalog", count: providers.data?.items.length }]} value={tab} onChange={setTab} />
      {tab === "connected" ? (
        <Card>
          {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
            <EmptyState icon={<Plug className="h-5 w-5" />} title="No integrations connected" description="Connect the services your automations need, such as email, messaging, CRM or an AI provider."
              action={manage && <Button variant="primary" onClick={() => setTab("catalog")}>Browse catalog</Button>} />
          ) : (
            <Table>
              <thead><tr><Th>Integration</Th><Th>Status</Th><Th>Last check</Th><Th>Detail</Th><Th /></tr></thead>
              <tbody>{list.data!.items.map((i) => (
                <tr key={i.id}>
                  <Td><span className="font-medium">{i.name}</span><div className="text-xs text-muted">{i.provider_name} - {AUTH_LABEL[i.auth_type] ?? i.auth_type}</div></Td>
                  <Td><StatusBadge status={i.status} /></Td>
                  <Td className="text-xs text-muted">{fmtRelative(i.last_checked_at)}{i.last_ok_at && i.status !== "connected" && <div>last OK {fmtRelative(i.last_ok_at)}</div>}</Td>
                  <Td className="max-w-xs text-xs">{tested[i.id] ? <span className={tested[i.id].startsWith("OK") ? "text-success" : "text-danger"}>{tested[i.id]}</span> : i.last_error ? <span className="text-danger">{i.last_error}</span>
                    : i.status === "pending" ? <span className="text-warning">Authorization not completed</span> : <span className="text-muted">-</span>}</Td>
                  <Td>{manage && (
                    <div className="flex justify-end gap-1.5">
                      {i.status !== "revoked" && <Button size="sm" loading={test.isPending && test.variables === i.id} onClick={() => test.mutate(i.id)}><RefreshCw className="h-3.5 w-3.5" /> Test</Button>}
                      {byKey[i.provider_key] && <Button size="sm" onClick={() => setConnecting({ provider: byKey[i.provider_key], existing: i })}>{i.auth_type === "oauth2" ? (i.status === "connected" ? "Reauthorize" : "Authorize") : "Update"}</Button>}
                      {i.status !== "revoked" && <Button size="sm" onClick={() => window.confirm(`Revoke ${i.name}? Stored credentials are deleted and workflows using it will fail.`) && revoke.mutate(i.id)}>Revoke</Button>}
                      <Button size="sm" variant="danger" onClick={() => window.confirm(`Delete ${i.name}?`) && remove.mutate(i.id)}>Delete</Button>
                    </div>)}</Td>
                </tr>
              ))}</tbody>
            </Table>
          )}
          <div className="px-4 py-2"><InlineError error={test.error ?? revoke.error ?? remove.error} /></div>
        </Card>
      ) : providers.isLoading ? <Spinner /> : providers.error ? <ErrorState error={providers.error} /> : (
        <>
          <div className="mb-4 flex flex-wrap gap-2">
            <Button size="sm" variant={category === "" ? "primary" : "secondary"} onClick={() => setCategory("")}>All</Button>
            {categories.map((c) => <Button key={c} size="sm" variant={category === c ? "primary" : "secondary"} onClick={() => setCategory(c)}>{c}</Button>)}
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {providers.data!.items.filter((p) => !category || p.category === category).map((p) => {
              const count = (list.data?.items ?? []).filter((i) => i.provider_key === p.key && i.status === "connected").length;
              return (
                <Card key={p.key} className="flex flex-col p-4">
                  <div className="flex items-start justify-between gap-2"><div className="font-medium">{p.name}</div><Badge>{AUTH_LABEL[p.auth_type] ?? p.auth_type}</Badge></div>
                  <div className="mt-1 text-xs text-muted">{p.category}{p.actions.length > 0 && ` - ${p.actions.map((a: string) => a.replace(/_/g, " ")).join(", ")}`}</div>
                  <div className="mt-3 flex flex-1 items-end justify-between gap-2">
                    {!p.available ? <span className="text-xs text-warning">Awaiting platform credentials</span> : count > 0 ? <span className="text-xs text-success">{count} connected</span> : <span className="text-xs text-muted">Not connected</span>}
                    {manage && <Button size="sm" variant={p.available ? "primary" : "secondary"} onClick={() => setConnecting({ provider: p })}>{p.available ? "Connect" : "Details"}</Button>}
                  </div>
                </Card>
              );
            })}
          </div>
        </>
      )}
      <ConnectDialog provider={connecting?.provider ?? null} existing={connecting?.existing} onClose={() => setConnecting(null)} />
    </>
  );
}

export default function IntegrationsPage() {
  return <Suspense fallback={<Spinner />}><IntegrationsInner /></Suspense>;
}
