"use client";

import Link from "next/link";
import { use, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useSession } from "@/lib/session";
import { fmtRelative, titleCase } from "@/lib/format";
import { Button, Card, CardHeader, Dialog, ErrorState, Field, InlineError, Input, Notice, PageHeader, Select, Spinner, StatusBadge, Table, Tabs, Td, Textarea, Th } from "@/components/ui";

const CLIENT_ROLES = ["CLIENT_ADMIN", "CLIENT_MEMBER", "VIEWER"];

function Profile({ org }: { org: any }) {
  const qc = useQueryClient();
  const session = useSession();
  const canManage = org.permissions.includes("org.manage");
  const [form, setForm] = useState({ name: org.name, industry: org.industry ?? "", mode: org.mode, status: org.status });
  const [client, setClient] = useState({ ...org.client });
  useEffect(() => { setForm({ name: org.name, industry: org.industry ?? "", mode: org.mode, status: org.status }); setClient({ ...org.client }); }, [org]);
  const done = async () => { await Promise.all([qc.invalidateQueries({ queryKey: ["organization", org.id] }), qc.invalidateQueries({ queryKey: ["organizations"] }), session.refresh()]); };
  const saveOrg = useMutation({
    mutationFn: () => api(`/organizations/${org.id}`, { method: "PATCH", org: false, body: {
      name: form.name, industry: form.industry || null, ...(session.isStaff ? { mode: form.mode, status: form.status } : {}) } }), onSuccess: done });
  const saveClient = useMutation({
    mutationFn: () => {
      const { account_manager_id: _a, status, ...rest } = client;
      return api(`/organizations/${org.id}/client`, { method: "PUT", org: false, body: { ...rest, ...(session.isStaff ? { status } : {}) } });
    }, onSuccess: done });
  const c = (k: string) => ({ value: client?.[k] ?? "", disabled: !canManage, onChange: (e: any) => setClient((x: any) => ({ ...x, [k]: e.target.value })) });

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card>
        <CardHeader title="Organization" />
        <div className="space-y-3 p-4">
          <Field label="Name"><Input value={form.name} disabled={!canManage} onChange={(e) => setForm({ ...form, name: e.target.value })} /></Field>
          <Field label="Industry"><Input value={form.industry} disabled={!canManage} onChange={(e) => setForm({ ...form, industry: e.target.value })} /></Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Operating mode" hint={session.isStaff ? undefined : "Managed by Weakless"}>
              <Select value={form.mode} disabled={!session.isStaff} onChange={(e) => setForm({ ...form, mode: e.target.value })}><option value="agency">Agency managed</option><option value="saas">Self-serve (SaaS)</option></Select></Field>
            <Field label="Status" hint={session.isStaff ? "Suspending blocks client sign-in to this organization." : "Managed by Weakless"}>
              <Select value={form.status} disabled={!session.isStaff} onChange={(e) => setForm({ ...form, status: e.target.value })}><option value="active">Active</option><option value="suspended">Suspended</option></Select></Field>
          </div>
          <InlineError error={saveOrg.error} />
          {canManage && <Button variant="primary" loading={saveOrg.isPending} disabled={form.name.trim().length < 2} onClick={() => saveOrg.mutate()}>Save organization</Button>}
        </div>
      </Card>
      <Card>
        <CardHeader title="Client profile" description="Business contact details" />
        <div className="space-y-3 p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Legal name"><Input {...c("legal_name")} /></Field>
            <Field label="Lifecycle status" hint={session.isStaff ? undefined : "Managed by Weakless"}>
              <Select {...c("status")} disabled={!session.isStaff}>{["lead", "onboarding", "active", "paused", "churned"].map((s) => <option key={s} value={s}>{titleCase(s)}</option>)}</Select></Field>
            <Field label="Contact name"><Input {...c("contact_name")} /></Field>
            <Field label="Contact email"><Input type="email" {...c("contact_email")} /></Field>
            <Field label="Phone"><Input {...c("contact_phone")} /></Field>
            <Field label="Country"><Input {...c("country")} /></Field>
            <Field label="Timezone" hint="IANA name, e.g. Europe/Sofia"><Input {...c("timezone")} /></Field>
          </div>
          {session.isStaff && <Field label="Internal notes"><Textarea {...c("notes")} /></Field>}
          <InlineError error={saveClient.error} />
          {canManage && <Button variant="primary" loading={saveClient.isPending} onClick={() => saveClient.mutate()}>Save profile</Button>}
        </div>
      </Card>
    </div>
  );
}

function Members({ org }: { org: any }) {
  const qc = useQueryClient();
  const { me } = useSession();
  const canManage = org.permissions.includes("members.manage");
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState({ email: "", full_name: "", role: "CLIENT_MEMBER", password: "" });
  const members = useQuery({ queryKey: ["members", org.id], queryFn: () => api<Page<any>>("/members", { org: org.id, params: { limit: 200 } }) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["members", org.id] });
  const add = useMutation({
    mutationFn: () => api("/members", { method: "POST", org: org.id, body: { email: form.email, full_name: form.full_name, role: form.role, password: form.password || null } }),
    onSuccess: () => { refresh(); setAdding(false); setForm({ email: "", full_name: "", role: "CLIENT_MEMBER", password: "" }); } });
  const update = useMutation({ mutationFn: (v: { id: string; body: any }) => api(`/members/${v.id}`, { method: "PATCH", org: org.id, body: v.body }), onSuccess: refresh });
  const remove = useMutation({ mutationFn: (id: string) => api(`/members/${id}`, { method: "DELETE", org: org.id }), onSuccess: refresh });

  return (
    <Card>
      <CardHeader title="Members" description="People from the client's team who can sign in to this organization. Weakless staff have access through their platform role."
        actions={canManage && <Button size="sm" variant="primary" onClick={() => setAdding(true)}><Plus className="h-3.5 w-3.5" /> Add member</Button>} />
      {members.isLoading ? <Spinner /> : members.error ? <div className="p-4"><ErrorState error={members.error} /></div> : members.data!.items.length === 0 ? (
        <p className="px-4 py-10 text-center text-sm text-muted">No client members yet.</p>
      ) : (
        <Table>
          <thead><tr><Th>Member</Th><Th>Role</Th><Th>Status</Th><Th>Last sign-in</Th><Th /></tr></thead>
          <tbody>{members.data!.items.map((m) => (
            <tr key={m.id}>
              <Td><div className="font-medium">{m.full_name || m.email}</div><div className="text-xs text-muted">{m.email}</div></Td>
              <Td>{canManage && m.user_id !== me.user.id
                ? <Select aria-label="Role" value={m.role} className="h-8 w-40 text-xs" onChange={(e) => update.mutate({ id: m.id, body: { role: e.target.value } })}>{CLIENT_ROLES.map((r) => <option key={r} value={r}>{titleCase(r.toLowerCase())}</option>)}</Select>
                : <span className="text-xs">{titleCase(m.role.toLowerCase())}</span>}</Td>
              <Td><StatusBadge status={m.status} /></Td>
              <Td className="text-xs text-muted">{fmtRelative(m.last_login_at)}</Td>
              <Td className="text-right">{canManage && m.user_id !== me.user.id && (
                <div className="flex justify-end gap-1.5">
                  <Button size="sm" onClick={() => update.mutate({ id: m.id, body: { status: m.status === "active" ? "disabled" : "active" } })}>{m.status === "active" ? "Disable" : "Enable"}</Button>
                  <Button size="sm" variant="danger" aria-label={`Remove ${m.email}`} onClick={() => window.confirm(`Remove ${m.email} from ${org.name}?`) && remove.mutate(m.id)}><Trash2 className="h-3.5 w-3.5" /></Button>
                </div>)}</Td>
            </tr>
          ))}</tbody>
        </Table>
      )}
      <div className="px-4 pb-3"><InlineError error={update.error ?? remove.error} /></div>
      <Dialog open={adding} onClose={() => setAdding(false)} title="Add member"
        footer={<><Button onClick={() => setAdding(false)}>Cancel</Button><Button variant="primary" loading={add.isPending} disabled={!form.email} onClick={() => add.mutate()}>Add member</Button></>}>
        <Field label="Email" required><Input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} autoFocus /></Field>
        <Field label="Full name"><Input value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} /></Field>
        <Field label="Role"><Select value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>{CLIENT_ROLES.map((r) => <option key={r} value={r}>{titleCase(r.toLowerCase())}</option>)}</Select></Field>
        <Field label="Initial password" hint="Required for people who do not have an account yet. At least 10 characters with an uppercase letter and a digit. Share it securely; they can change it under Settings.">
          <Input type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></Field>
        <InlineError error={add.error} />
      </Dialog>
    </Card>
  );
}

export default function OrganizationPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const session = useSession();
  const qc = useQueryClient();
  const [tab, setTab] = useState<"profile" | "members">("profile");
  const org = useQuery({ queryKey: ["organization", id], queryFn: () => api(`/organizations/${id}`, { org: false }) });
  const archive = useMutation({ mutationFn: () => api(`/organizations/${id}`, { method: "DELETE", org: false }),
    onSuccess: async () => { if (session.org?.id === id) session.selectOrg(null); await qc.invalidateQueries({ queryKey: ["organizations"] }); await session.refresh(); router.push("/organizations"); } });

  if (org.isLoading) return <Spinner />;
  if (org.error) return <ErrorState error={org.error} onRetry={() => org.refetch()} />;
  const o = org.data;
  const canMembers = o.permissions.includes("members.read");
  return (
    <>
      <div className="mb-2 text-xs text-muted"><Link href="/organizations" className="hover:text-fg">Organizations</Link> / {o.name}</div>
      <PageHeader title={o.name} description={<span className="flex items-center gap-2"><StatusBadge status={o.status} /> {o.mode === "saas" ? "Self-serve (SaaS)" : "Agency managed"} - your role: {titleCase(o.role.toLowerCase())}</span>}
        actions={<>
          {session.org?.id !== id && <Button onClick={() => session.selectOrg(id)}>Work in this organization</Button>}
          {session.can("platform.organizations.create") && <Button variant="danger" loading={archive.isPending}
            onClick={() => window.confirm(`Archive ${o.name}? Its workflows are deactivated and client access ends. Data is retained.`) && archive.mutate()}>Archive</Button>}
        </>} />
      <InlineError error={archive.error} />
      {o.status === "suspended" && <div className="mb-4"><Notice tone="warning">This organization is suspended. Client members cannot sign in to it until it is reactivated.</Notice></div>}
      <Tabs tabs={[{ key: "profile", label: "Profile" }, ...(canMembers ? [{ key: "members" as const, label: "Members" }] : [])]} value={tab} onChange={setTab} />
      {tab === "profile" ? <Profile org={o} /> : <Members org={o} />}
    </>
  );
}
