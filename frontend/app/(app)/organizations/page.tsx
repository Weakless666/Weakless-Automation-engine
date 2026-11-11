"use client";

import Link from "next/link";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, Plus } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useSession } from "@/lib/session";
import { fmtDay, titleCase } from "@/lib/format";
import { Button, Card, Dialog, EmptyState, ErrorState, Field, InlineError, Input, PageHeader, Pager, Select, Spinner, StatusBadge, Table, Td, Th } from "@/components/ui";

const INDUSTRIES = ["hospitality", "restaurants", "beauty", "real_estate", "ecommerce", "digital_agency", "healthcare", "travel", "other"];

function CreateOrg({ open, onClose }: { open: boolean; onClose: () => void }) {
  const qc = useQueryClient();
  const session = useSession();
  const [form, setForm] = useState({ name: "", industry: "", mode: "agency", plan_key: "", contact_email: "" });
  const plans = useQuery({ queryKey: ["plans"], queryFn: () => api<{ items: any[] }>("/billing/plans", { org: false }), enabled: open });
  const create = useMutation({
    mutationFn: () => api("/organizations", { method: "POST", org: false, body: {
      name: form.name, industry: form.industry || null, mode: form.mode, plan_key: form.plan_key || null,
      client: form.contact_email ? { contact_email: form.contact_email } : null } }),
    onSuccess: async (org: any) => {
      await Promise.all([qc.invalidateQueries({ queryKey: ["organizations"] }), session.refresh()]);
      setForm({ name: "", industry: "", mode: "agency", plan_key: "", contact_email: "" });
      onClose();
      session.selectOrg(org.id);
    },
  });
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm((f) => ({ ...f, [k]: e.target.value }));
  return (
    <Dialog open={open} onClose={onClose} title="New organization" description="Creates the tenant with a default workspace, a client profile and a subscription."
      footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={form.name.trim().length < 2} onClick={() => create.mutate()}>Create organization</Button></>}>
      <Field label="Business name" required><Input value={form.name} onChange={set("name")} autoFocus /></Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Industry"><Select value={form.industry} onChange={set("industry")}><option value="">Not set</option>{INDUSTRIES.map((i) => <option key={i} value={i}>{titleCase(i)}</option>)}</Select></Field>
        <Field label="Operating mode" hint={form.mode === "agency" ? "Weakless builds and manages everything." : "The client can install templates and manage integrations."}>
          <Select value={form.mode} onChange={set("mode")}><option value="agency">Agency managed</option><option value="saas">Self-serve (SaaS)</option></Select></Field>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Plan" hint="Default plan when not set"><Select value={form.plan_key} onChange={set("plan_key")}><option value="">Default</option>{(plans.data?.items ?? []).filter((p) => p.is_active).map((p) => <option key={p.key} value={p.key}>{p.name}</option>)}</Select></Field>
        <Field label="Contact email"><Input type="email" value={form.contact_email} onChange={set("contact_email")} /></Field>
      </div>
      <InlineError error={create.error} />
    </Dialog>
  );
}

export default function OrganizationsPage() {
  const { can, isStaff } = useSession();
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const list = useQuery({ queryKey: ["organizations", q, offset], queryFn: () => api<Page<any>>("/organizations", { params: { q, limit: 25, offset }, org: false }) });
  const canCreate = can("platform.organizations.create");

  return (
    <>
      <PageHeader title="Organizations" description={isStaff ? "Every client organization on the platform. Each is an isolated tenant." : "Organizations you are a member of."}
        actions={canCreate && <Button variant="primary" onClick={() => setCreating(true)}><Plus className="h-4 w-4" /> New organization</Button>} />
      <Card>
        <div className="border-b p-3"><Input placeholder="Search by name..." value={q} onChange={(e) => { setQ(e.target.value); setOffset(0); }} className="max-w-xs" aria-label="Search organizations" /></div>
        {list.isLoading ? <Spinner /> : list.error ? <div className="p-4"><ErrorState error={list.error} onRetry={() => list.refetch()} /></div> : list.data!.items.length === 0 ? (
          <EmptyState icon={<Building2 className="h-5 w-5" />} title={q ? "No organizations match your search" : "No organizations yet"}
            description={canCreate && !q ? "Create the first client organization to start building automations for it." : undefined}
            action={canCreate && !q ? <Button variant="primary" onClick={() => setCreating(true)}>New organization</Button> : undefined} />
        ) : (
          <>
            <Table>
              <thead><tr><Th>Organization</Th><Th>Mode</Th><Th>Status</Th><Th>Client</Th><Th className="text-right">Members</Th><Th className="text-right">Active workflows</Th><Th className="text-right">Created</Th></tr></thead>
              <tbody>{list.data!.items.map((o) => (
                <tr key={o.id} className="hover:bg-raised/50">
                  <Td><Link href={`/organizations/${o.id}`} className="font-medium hover:text-primary">{o.name}</Link><div className="text-xs text-muted">{o.industry ? titleCase(o.industry) : o.slug}</div></Td>
                  <Td className="text-xs text-muted">{o.mode === "saas" ? "Self-serve" : "Agency managed"}</Td>
                  <Td><StatusBadge status={o.status} /></Td>
                  <Td><StatusBadge status={o.client_status} /></Td>
                  <Td className="tabular text-right">{o.member_count ?? "-"}</Td>
                  <Td className="tabular text-right">{o.active_workflows ?? "-"}</Td>
                  <Td className="text-right text-xs text-muted">{fmtDay(o.created_at)}</Td>
                </tr>
              ))}</tbody>
            </Table>
            <Pager total={list.data!.total} limit={25} offset={offset} onChange={setOffset} />
          </>
        )}
      </Card>
      <CreateOrg open={creating} onClose={() => setCreating(false)} />
    </>
  );
}
