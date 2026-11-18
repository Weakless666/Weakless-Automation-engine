"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { IssuedLink, passwordProblem } from "@/components/auth-card";
import { MfaSetup, RecoveryCodes } from "@/components/mfa";
import { useSession } from "@/lib/session";
import { fmtDate, fmtRelative, titleCase } from "@/lib/format";
import { Badge, Button, Card, CardHeader, Checkbox, Dialog, ErrorState, Field, InlineError, Input, Notice, PageHeader, Select, Spinner, StatusBadge, Table, Tabs, Td, Textarea, Th } from "@/components/ui";

const STAFF_ROLES = ["SUPER_ADMIN", "ADMIN", "MANAGER", "AUTOMATION_ENGINEER", "SALES", "SUPPORT"];

function Account() {
  const session = useSession();
  const qc = useQueryClient();
  const [name, setName] = useState(session.me.user.full_name);
  const [pw, setPw] = useState({ current_password: "", new_password: "" });
  const sessions = useQuery({ queryKey: ["sessions"], queryFn: () => api<{ items: any[] }>("/auth/sessions", { org: false }) });
  const history = useQuery({ queryKey: ["login-history"], queryFn: () => api<Page<any>>("/auth/login-history", { org: false, params: { limit: 10 } }) });
  const profile = useMutation({ mutationFn: () => api("/auth/me", { method: "PATCH", org: false, body: { full_name: name } }), onSuccess: () => session.refresh() });
  const password = useMutation({ mutationFn: () => api("/auth/change-password", { method: "POST", org: false, body: pw }), onSuccess: () => { setPw({ current_password: "", new_password: "" }); qc.invalidateQueries({ queryKey: ["sessions"] }); } });
  const revoke = useMutation({ mutationFn: (id: string) => api(`/auth/sessions/${id}`, { method: "DELETE", org: false }), onSuccess: () => qc.invalidateQueries({ queryKey: ["sessions"] }) });
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card><CardHeader title="Profile" />
        <div className="space-y-3 p-4">
          <Field label="Email"><Input value={session.me.user.email} disabled /></Field>
          <Field label="Full name"><Input value={name} onChange={(e) => setName(e.target.value)} /></Field>
          <InlineError error={profile.error} />
          <Button variant="primary" loading={profile.isPending} disabled={!name.trim() || name === session.me.user.full_name} onClick={() => profile.mutate()}>Save profile</Button>
        </div></Card>
      <Card><CardHeader title="Password" description="Changing it signs out your other sessions." />
        <form className="space-y-3 p-4" onSubmit={(e) => { e.preventDefault(); password.mutate(); }}>
          <Field label="Current password"><Input type="password" autoComplete="current-password" value={pw.current_password} onChange={(e) => setPw({ ...pw, current_password: e.target.value })} /></Field>
          <Field label="New password" hint="At least 10 characters with an uppercase letter and a digit."><Input type="password" autoComplete="new-password" value={pw.new_password} onChange={(e) => setPw({ ...pw, new_password: e.target.value })} /></Field>
          <InlineError error={password.error} />
          {password.isSuccess && <Notice tone="success">Password changed.</Notice>}
          <Button type="submit" variant="primary" loading={password.isPending} disabled={!pw.current_password || pw.new_password.length < 10}>Change password</Button>
        </form></Card>
      <Security />
      <Card><CardHeader title="Active sessions" />
        {sessions.isLoading ? <Spinner /> : sessions.error ? <div className="p-4"><ErrorState error={sessions.error} /></div> : (
          <Table><tbody>{sessions.data!.items.map((s) => (
            <tr key={s.id}><Td><div className="max-w-xs truncate text-xs">{s.user_agent || "Unknown device"}</div><div className="text-xs text-muted">{s.ip ?? "-"} - active {fmtRelative(s.last_seen_at ?? s.created_at)}</div></Td>
              <Td className="text-right">{s.current ? <Badge tone="primary">This session</Badge> : <Button size="sm" variant="danger" loading={revoke.isPending && revoke.variables === s.id} onClick={() => revoke.mutate(s.id)}>Sign out</Button>}</Td></tr>))}</tbody></Table>)}
        <div className="px-4 py-2"><InlineError error={revoke.error} /></div></Card>
      <Card><CardHeader title="Sign-in history" />
        {history.isLoading ? <Spinner /> : history.error ? <div className="p-4"><ErrorState error={history.error} /></div> : (
          <Table><tbody>{history.data!.items.map((h) => (
            <tr key={h.id}><Td className="text-xs text-muted">{fmtDate(h.created_at)}</Td><Td>{h.success ? <Badge tone="success">Success</Badge> : <Badge tone="danger">Failed</Badge>}</Td>
              <Td className="font-mono text-2xs text-muted">{h.ip ?? "-"}</Td><Td className="text-xs text-muted">{h.reason?.replace(/_/g, " ") ?? ""}</Td></tr>))}</tbody></Table>)}</Card>
    </div>
  );
}

function Security() {
  const qc = useQueryClient();
  const session = useSession();
  const status = useQuery({ queryKey: ["mfa"], queryFn: () => api<{ enabled: boolean; required: boolean; enabled_at: string | null; recovery_codes_left: number }>("/auth/mfa", { org: false }) });
  const [mode, setMode] = useState<null | "enable" | "codes" | "disable">(null);
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const done = () => { setMode(null); setCode(""); setPassword(""); setCodes(null); qc.invalidateQueries({ queryKey: ["mfa"] }); session.refresh(); };
  const regen = useMutation({ mutationFn: () => api<{ recovery_codes: string[] }>("/auth/mfa/recovery-codes", { method: "POST", org: false, body: { code } }), onSuccess: (r) => setCodes(r.recovery_codes) });
  const disable = useMutation({ mutationFn: () => api("/auth/mfa/disable", { method: "POST", org: false, body: { password, code } }), onSuccess: done });
  const s = status.data;
  return (
    <Card><CardHeader title="Two-factor sign-in" description="A code from an authenticator app on your phone, in addition to your password." />
      {status.isLoading ? <Spinner /> : status.error ? <div className="p-4"><ErrorState error={status.error} /></div> : s && (
        <div className="space-y-3 p-4">
          <div className="flex items-center gap-2 text-sm">{s.enabled ? <Badge tone="success">On</Badge> : <Badge tone="warning">Off</Badge>}
            {s.enabled && <span className="text-xs text-muted">since {fmtDate(s.enabled_at)} - {s.recovery_codes_left} recovery codes left</span>}</div>
          {s.required && <p className="text-xs text-muted">Required for Weakless team accounts.</p>}
          {s.enabled && s.recovery_codes_left <= 3 && <Notice tone="warning">You are running out of recovery codes. Create new ones.</Notice>}
          <div className="flex flex-wrap gap-2">
            {!s.enabled && <Button variant="primary" size="sm" onClick={() => setMode("enable")}>Turn on</Button>}
            {s.enabled && <Button size="sm" onClick={() => setMode("codes")}>New recovery codes</Button>}
            {s.enabled && !s.required && <Button size="sm" variant="danger" onClick={() => setMode("disable")}>Turn off</Button>}
          </div>
        </div>
      )}
      <Dialog open={mode === "enable"} onClose={done} title="Turn on two-factor sign-in">
        {mode === "enable" && <MfaSetup start={() => api("/auth/mfa/enroll", { method: "POST", org: false })}
          confirm={(c) => api("/auth/mfa/enroll/confirm", { method: "POST", org: false, body: { code: c } })} onDone={done} />}
      </Dialog>
      <Dialog open={mode === "codes"} onClose={done} title="New recovery codes" description="Your current recovery codes stop working.">
        {codes ? <RecoveryCodes codes={codes} onContinue={done} continueLabel="Done" /> : (
          <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); regen.mutate(); }}>
            <Field label="Current code" hint="From your authenticator app, or an unused recovery code."><Input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" className="font-mono" /></Field>
            <InlineError error={regen.error} />
            <Button type="submit" variant="primary" loading={regen.isPending} disabled={code.trim().length < 6}>Create new codes</Button>
          </form>
        )}
      </Dialog>
      <Dialog open={mode === "disable"} onClose={done} title="Turn off two-factor sign-in" description="Your account will be protected by your password only.">
        <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); disable.mutate(); }}>
          <Field label="Password"><Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} /></Field>
          <Field label="Current code"><Input value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" className="font-mono" /></Field>
          <InlineError error={disable.error} />
          <Button type="submit" variant="danger" loading={disable.isPending} disabled={!password || code.trim().length < 6}>Turn off</Button>
        </form>
      </Dialog>
    </Card>
  );
}

function Team() {
  const qc = useQueryClient();
  const { me } = useSession();
  const blank = { email: "", full_name: "", password: "", platform_role: "AUTOMATION_ENGINEER", method: "invite" as "invite" | "password" };
  const [open, setOpen] = useState(false);
  const [f, setF] = useState(blank);
  const [issued, setIssued] = useState<{ title: string; kind: "invitation" | "reset"; link: any } | null>(null);
  const users = useQuery({ queryKey: ["staff"], queryFn: () => api<Page<any>>("/users", { org: false, params: { limit: 200 } }) });
  const refresh = () => qc.invalidateQueries({ queryKey: ["staff"] });
  const create = useMutation({
    mutationFn: () => api("/users", { method: "POST", org: false, body: { email: f.email, full_name: f.full_name, platform_role: f.platform_role, password: f.method === "password" ? f.password : null } }),
    onSuccess: (u: any) => { refresh(); setOpen(false); setF(blank); if (u.invitation) setIssued({ title: `Invitation for ${u.email}`, kind: "invitation", link: u.invitation }); } });
  const update = useMutation({ mutationFn: (v: { id: string; body: any }) => api(`/users/${v.id}`, { method: "PATCH", org: false, body: v.body }), onSuccess: refresh });
  const resend = useMutation({ mutationFn: (u: any) => api(`/users/${u.id}/invitation`, { method: "POST", org: false }),
    onSuccess: (link: any, u: any) => setIssued({ title: `New invitation for ${u.email}`, kind: "invitation", link }) });
  const resetMfa = useMutation({ mutationFn: (u: any) => api(`/users/${u.id}/mfa-reset`, { method: "POST", org: false }), onSuccess: refresh });
  const resetLink = useMutation({ mutationFn: (u: any) => api(`/users/${u.id}/reset-link`, { method: "POST", org: false }),
    onSuccess: (link: any, u: any) => setIssued({ title: `Password reset link for ${u.email}`, kind: "reset", link: { ...link, email_sent: false } }) });
  const passwordIssue = f.method === "password" ? passwordProblem(f.password, f.password) : null;
  return (
    <Card><CardHeader title="Weakless team" description="Staff accounts have their role in every organization." actions={<Button size="sm" variant="primary" onClick={() => setOpen(true)}><Plus className="h-3.5 w-3.5" /> Add team member</Button>} />
      {users.isLoading ? <Spinner /> : users.error ? <div className="p-4"><ErrorState error={users.error} /></div> : (
        <Table><thead><tr><Th>User</Th><Th>Role</Th><Th>Status</Th><Th>2FA</Th><Th>Last sign-in</Th><Th /></tr></thead>
          <tbody>{users.data!.items.map((u) => {
            const self = u.id === me.user.id;
            const locked = u.platform_role === "SUPER_ADMIN" && me.user.platform_role !== "SUPER_ADMIN";
            return (
              <tr key={u.id}><Td><div className="font-medium">{u.full_name}</div><div className="text-xs text-muted">{u.email}</div></Td>
                <Td>{self || locked ? <span className="text-xs">{titleCase(u.platform_role.toLowerCase())}</span> : (
                  <Select aria-label="Role" className="h-8 w-48 text-xs" value={u.platform_role} onChange={(e) => update.mutate({ id: u.id, body: { platform_role: e.target.value } })}>
                    {STAFF_ROLES.filter((r) => r !== "SUPER_ADMIN" || me.user.platform_role === "SUPER_ADMIN").map((r) => <option key={r} value={r}>{titleCase(r.toLowerCase())}</option>)}</Select>)}</Td>
                <Td><StatusBadge status={u.invitation_pending ? "invited" : u.is_active ? "active" : "disabled"} /></Td>
                <Td>{u.mfa_enabled ? <Badge tone="success">On</Badge> : <Badge tone="warning">Off</Badge>}</Td><Td className="text-xs text-muted">{fmtRelative(u.last_login_at)}</Td>
                <Td className="text-right">{!self && !locked && (
                  <div className="flex justify-end gap-1.5">
                    {u.invitation_pending ? <Button size="sm" loading={resend.isPending && resend.variables?.id === u.id}
                      onClick={() => window.confirm(`Send a new invitation to ${u.email}? The previous link stops working.`) && resend.mutate(u)}>Resend invite</Button>
                      : u.is_active && <Button size="sm" loading={resetLink.isPending && resetLink.variables?.id === u.id}
                        onClick={() => window.confirm(`Create a password reset link for ${u.email}? It is recorded in the audit log.`) && resetLink.mutate(u)}>Reset link</Button>}
                    {u.mfa_enabled && <Button size="sm" loading={resetMfa.isPending && resetMfa.variables?.id === u.id}
                      onClick={() => window.confirm(`Reset two-factor sign-in for ${u.email}? Use this when their phone is lost. They are signed out and set it up again.`) && resetMfa.mutate(u)}>Reset 2FA</Button>}
                    <Button size="sm" onClick={() => update.mutate({ id: u.id, body: { is_active: !u.is_active } })}>{u.is_active ? "Disable" : "Enable"}</Button>
                  </div>)}</Td></tr>);
          })}</tbody></Table>)}
      <div className="px-4 py-2"><InlineError error={update.error ?? resend.error ?? resetLink.error ?? resetMfa.error} /></div>
      <Dialog open={open} onClose={() => setOpen(false)} title="Add team member"
        footer={<><Button onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="primary" loading={create.isPending} disabled={!f.email.includes("@") || !f.full_name.trim() || !!passwordIssue} onClick={() => create.mutate()}>{f.method === "invite" ? "Send invitation" : "Create account"}</Button></>}>
        <Field label="Email" required><Input type="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></Field>
        <Field label="Full name" required><Input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></Field>
        <Field label="Role"><Select value={f.platform_role} onChange={(e) => setF({ ...f, platform_role: e.target.value })}>{STAFF_ROLES.filter((r) => r !== "SUPER_ADMIN" || me.user.platform_role === "SUPER_ADMIN").map((r) => <option key={r} value={r}>{titleCase(r.toLowerCase())}</option>)}</Select></Field>
        <fieldset className="space-y-2">
          <legend className="mb-1 text-xs font-medium text-muted">How should they get access?</legend>
          <label className="flex cursor-pointer items-start gap-2 text-sm"><input type="radio" name="staff-method" className="mt-0.5 accent-[rgb(var(--primary))]" checked={f.method === "invite"} onChange={() => setF({ ...f, method: "invite" })} />
            <span>Send an invitation<span className="block text-xs text-muted">They choose their own password.</span></span></label>
          <label className="flex cursor-pointer items-start gap-2 text-sm"><input type="radio" name="staff-method" className="mt-0.5 accent-[rgb(var(--primary))]" checked={f.method === "password"} onChange={() => setF({ ...f, method: "password" })} />
            <span>Set a password for them</span></label>
        </fieldset>
        {f.method === "password" && <Field label="Initial password" error={f.password && passwordIssue ? passwordIssue : null} hint="At least 10 characters with an uppercase letter and a digit.">
          <Input type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} /></Field>}
        <InlineError error={create.error} />
      </Dialog>
      <Dialog open={!!issued} onClose={() => setIssued(null)} title={issued?.title ?? ""} footer={<Button variant="primary" onClick={() => setIssued(null)}>Done</Button>}>
        {issued && <IssuedLink link={issued.link} kind={issued.kind} />}
      </Dialog>
    </Card>
  );
}

function Roles({ editable }: { editable: boolean }) {
  const qc = useQueryClient();
  const roles = useQuery({ queryKey: ["roles"], queryFn: () => api<{ roles: any[]; permissions: any[] }>("/roles", { org: false }) });
  const [role, setRole] = useState("AUTOMATION_ENGINEER");
  const [grants, setGrants] = useState<string[]>([]);
  const current = roles.data?.roles.find((r) => r.key === role);
  useEffect(() => { if (current) setGrants(current.permissions); }, [current]);
  const save = useMutation({ mutationFn: () => api(`/roles/${role}`, { method: "PUT", org: false, body: { permissions: grants } }), onSuccess: () => qc.invalidateQueries({ queryKey: ["roles"] }) });
  if (roles.isLoading) return <Spinner />;
  if (roles.error) return <ErrorState error={roles.error} />;
  const locked = !editable || role === "SUPER_ADMIN";
  const dirty = current && (grants.length !== current.permissions.length || grants.some((g) => !current.permissions.includes(g)));
  return (
    <Card><CardHeader title="Roles and permissions" description="What each role may do. Client roles can never receive platform permissions."
      actions={<Select aria-label="Role" value={role} onChange={(e) => setRole(e.target.value)} className="h-8 w-56 text-xs">{roles.data!.roles.map((r) => <option key={r.key} value={r.key}>{r.name} ({r.scope})</option>)}</Select>} />
      <div className="grid gap-x-6 gap-y-1.5 p-4 md:grid-cols-2">
        {roles.data!.permissions.filter((p) => current?.scope === "platform" || !p.key.startsWith("platform.")).map((p) => (
          <Checkbox key={p.key} disabled={locked} checked={grants.includes(p.key)} onChange={(on) => setGrants(on ? [...grants, p.key] : grants.filter((g) => g !== p.key))}
            label={<span><span className="font-mono text-xs">{p.key}</span><span className="ml-2 text-xs text-muted">{p.description}</span></span>} />))}
      </div>
      <div className="flex items-center gap-3 border-t px-4 py-3">
        {role === "SUPER_ADMIN" ? <span className="text-xs text-muted">Super Admin always has every permission.</span> : !editable ? <span className="text-xs text-muted">Only a Super Admin can change role permissions.</span>
          : <Button variant="primary" size="sm" loading={save.isPending} disabled={!dirty} onClick={() => window.confirm(`Change what every ${current?.name} can do?`) && save.mutate()}>Save permissions</Button>}
        <InlineError error={save.error} />
      </div>
    </Card>
  );
}

const RETENTION: { key: string; label: string; hint: string }[] = [
  { key: "execution_details_days", label: "Execution step logs and payloads", hint: "Status, timing and errors stay; retry is no longer possible." },
  { key: "executions_days", label: "Executions", hint: "Finished executions are deleted; analytics covers what remains." },
  { key: "events_days", label: "Inbound events", hint: "Webhook, form and app event log. Redeliveries older than this are not recognised as duplicates." },
  { key: "usage_detail_days", label: "Usage detail", hint: "Merged into daily totals per workflow; billing and quota numbers do not change." },
  { key: "notifications_days", label: "Notifications", hint: "In-app notifications and channel delivery logs." },
  { key: "queue_jobs_days", label: "Completed queue jobs", hint: "Dead letters are kept until handled." },
  { key: "files_days", label: "Files made by workflows", hint: "Downloads, email attachments and generated files. Applies to new files; uploads are kept until deleted." },
  { key: "audit_days", label: "Audit log and sign-in history", hint: "At least 30 days. Leave empty to keep forever." },
];

function Retention({ policy }: { policy: Record<string, number | null> | undefined }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Record<string, string>>({});
  useEffect(() => {
    if (policy) setDraft(Object.fromEntries(RETENTION.map(({ key }) => [key, policy[key] == null ? "" : String(policy[key])])));
  }, [policy]);
  const value = Object.fromEntries(RETENTION.map(({ key }) => [key, draft[key]?.trim() ? Number(draft[key]) : null]));
  const invalid = Object.values(value).some((v) => v !== null && (!Number.isInteger(v) || v < 1));
  const save = useMutation({ mutationFn: () => api("/settings/data.retention", { method: "PUT", org: false, body: { value } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }) });
  const runNow = useMutation({ mutationFn: () => api("/retention/run", { method: "POST", org: false }) });
  const changed = runNow.data ? Object.entries<number>(runNow.data.result).filter(([, n]) => n > 0) : [];
  return (
    <Card className="lg:col-span-2"><CardHeader title="Data retention" description="Days to keep operational history for every organization. Empty keeps it forever. Workers apply the policy hourly." />
      <div className="space-y-4 p-4">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {RETENTION.map(({ key, label, hint }) => (
            <Field key={key} label={label} hint={hint}>
              <Input type="number" min={1} inputMode="numeric" placeholder="Keep forever" value={draft[key] ?? ""} onChange={(e) => setDraft({ ...draft, [key]: e.target.value })} />
            </Field>))}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm" variant="primary" loading={save.isPending} disabled={invalid}
            onClick={() => window.confirm("Save this retention policy? Data older than these limits is deleted permanently at the next hourly run.") && save.mutate()}>Save retention</Button>
          <Button size="sm" loading={runNow.isPending} onClick={() => window.confirm("Apply the saved retention policy now? Older data is deleted permanently.") && runNow.mutate()}>Apply now</Button>
          {save.isSuccess && !save.isPending && <span className="text-xs text-success">Saved</span>}
          {runNow.data && <span className="text-xs text-muted" role="status">{changed.length ? changed.map(([k, n]) => `${k.replace(/_/g, " ")}: ${n}`).join(", ") : "Nothing was old enough to remove."}</span>}
        </div>
        <InlineError error={save.error ?? runNow.error} />
      </div></Card>
  );
}

function Platform() {
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api("/settings", { org: false }) });
  const [pricing, setPricing] = useState("");
  const [ai, setAi] = useState({ provider: "anthropic", model: "claude-opus-5-5" });
  const [plan, setPlan] = useState("internal");
  const [fallback, setFallback] = useState(true);
  const [staffMfa, setStaffMfa] = useState(true);
  const get = (k: string) => settings.data?.items.find((i: any) => i.key === k)?.value;
  useEffect(() => {
    if (!settings.data) return;
    setPricing(JSON.stringify(get("ai.model_pricing") ?? {}, null, 2));
    setAi(get("ai.default") ?? { provider: "anthropic", model: "claude-opus-5-5" });
    setPlan(get("billing.default_plan") ?? "internal");
    setFallback(get("ai.anthropic_refusal_fallback") ?? true);
    setStaffMfa(get("security.staff_mfa_required") ?? true);
  }, [settings.data]); // eslint-disable-line react-hooks/exhaustive-deps
  const put = useMutation({ mutationFn: (v: { key: string; value: unknown }) => api(`/settings/${v.key}`, { method: "PUT", org: false, body: { value: v.value } }), onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }) });
  let parsed: unknown = null;
  try { parsed = JSON.parse(pricing || "{}"); } catch { parsed = null; }
  if (settings.isLoading) return <Spinner />;
  if (settings.error) return <ErrorState error={settings.error} />;
  const env = settings.data.environment;
  const flag = (ok: boolean, yes = "Configured", no = "Not configured") => ok ? <Badge tone="success">{yes}</Badge> : <Badge tone="warning">{no}</Badge>;
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card className="lg:col-span-2"><CardHeader title="Sign-in security" />
        <div className="space-y-2 p-4">
          <Checkbox label="Require two-factor sign-in for Weakless team accounts" checked={staffMfa}
            onChange={(v) => { if (v || window.confirm("Turn off the two-factor requirement for the Weakless team? Team accounts can reach every client organization.")) { setStaffMfa(v); put.mutate({ key: "security.staff_mfa_required", value: v }); } }} />
          <p className="text-xs text-muted">Team members without it set it up at their next sign-in. Client users can switch it on for themselves under Settings.</p>
        </div></Card>
      <Retention policy={get("data.retention")} />
      <Card><CardHeader title="AI defaults" description="Used by AI steps that do not choose a provider or model." />
        <div className="space-y-3 p-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Provider"><Select value={ai.provider} onChange={(e) => setAi({ ...ai, provider: e.target.value })}>{["anthropic", "openai", "gemini", "openai_compatible"].map((p) => <option key={p} value={p}>{p.replace("_", " ")}</option>)}</Select></Field>
            <Field label="Model"><Input value={ai.model} onChange={(e) => setAi({ ...ai, model: e.target.value })} /></Field>
          </div>
          <Button size="sm" variant="primary" loading={put.isPending && put.variables?.key === "ai.default"} disabled={!ai.model.trim()} onClick={() => put.mutate({ key: "ai.default", value: ai })}>Save AI default</Button>
          <div className="border-t pt-3"><Checkbox label="Use Anthropic's server-side refusal fallback on models that support it" checked={fallback}
            onChange={(v) => { setFallback(v); put.mutate({ key: "ai.anthropic_refusal_fallback", value: v }); }} />
            <p className="mt-1 text-xs text-muted">When a supported Claude model declines a request for policy reasons, Anthropic re-runs it on its recommended fallback model.</p></div>
        </div></Card>
      <Card><CardHeader title="Model prices" description="USD per million tokens. Entries here add to or override the built-in Anthropic list prices. Models without a price show cost as unknown." />
        <div className="space-y-3 p-4">
          <Field label="Overrides (JSON)" error={parsed === null ? "Must be valid JSON" : null} hint='Example: {"my-model": {"input": 1.5, "output": 6}}'>
            <Textarea value={pricing} onChange={(e) => setPricing(e.target.value)} className="min-h-[120px] font-mono text-xs" spellCheck={false} /></Field>
          <Button size="sm" variant="primary" loading={put.isPending && put.variables?.key === "ai.model_pricing"} disabled={parsed === null} onClick={() => put.mutate({ key: "ai.model_pricing", value: parsed })}>Save prices</Button>
          <details className="text-xs text-muted"><summary className="cursor-pointer">Built-in prices</summary>
            <Table className="mt-2"><tbody>{Object.entries<any>(settings.data.defaults["ai.model_pricing"]).map(([m, p]) => <tr key={m}><Td className="font-mono text-xs">{m}</Td><Td className="tabular text-right">${p.input} in</Td><Td className="tabular text-right">${p.output} out</Td></tr>)}</tbody></Table></details>
        </div></Card>
      <Card><CardHeader title="New organizations" />
        <div className="space-y-3 p-4">
          <Field label="Default plan key" hint="Assigned when an organization is created without choosing a plan."><Input value={plan} onChange={(e) => setPlan(e.target.value)} /></Field>
          <Button size="sm" variant="primary" loading={put.isPending && put.variables?.key === "billing.default_plan"} disabled={!plan.trim()} onClick={() => put.mutate({ key: "billing.default_plan", value: plan })}>Save default plan</Button>
          <InlineError error={put.error} />
        </div></Card>
      <Card><CardHeader title="Environment" description="Set through environment variables on the server. Values are never displayed." />
        <dl className="divide-y divide-line/60 text-sm">
          {[["Environment", <Badge key="e">{env.env}</Badge>], ["Database", <Badge key="d" tone={env.database === "postgresql" ? "success" : "warning"}>{env.database}</Badge>],
            ["Queue worker", <Badge key="w">{env.embedded_worker ? "Embedded in API" : "Separate process"}</Badge>], ["Platform email (SMTP)", flag(env.smtp_configured)],
            ["Stripe billing", flag(env.stripe_configured)], ["Stripe webhook secret", flag(env.stripe_webhook_configured)], ["Error tracking (Sentry)", flag(env.sentry_configured)],
            ["Vault active key", <span key="v" className="font-mono text-xs">{env.vault_active_key_id} ({env.vault_key_ids.length} in ring)</span>],
            ...Object.entries<boolean>(env.platform_ai_keys).map(([k, v]) => [`Platform AI key: ${k}`, flag(v, "Set", "Not set")] as [string, React.ReactNode]),
            ...Object.entries<boolean>(env.oauth_clients).map(([k, v]) => [`OAuth app: ${k}`, flag(v)] as [string, React.ReactNode])].map(([l, v]) => (
            <div key={l as string} className="flex items-center justify-between px-4 py-2"><dt className="text-muted">{l}</dt><dd>{v}</dd></div>))}
        </dl></Card>
    </div>
  );
}

export default function SettingsPage() {
  const { can, isStaff } = useSession();
  const [tab, setTab] = useState<"account" | "team" | "roles" | "platform">("account");
  const tabs = [{ key: "account" as const, label: "My account" }, ...(can("platform.users.manage") ? [{ key: "team" as const, label: "Team" }] : []),
    ...(isStaff ? [{ key: "roles" as const, label: "Roles" }] : []), ...(can("platform.settings.manage") ? [{ key: "platform" as const, label: "Platform" }] : [])];
  return (
    <>
      <PageHeader title="Settings" />
      <Tabs tabs={tabs} value={tabs.some((t) => t.key === tab) ? tab : "account"} onChange={setTab} />
      {tab === "team" ? <Team /> : tab === "roles" ? <Roles editable={can("platform.settings.manage")} /> : tab === "platform" ? <Platform /> : <Account />}
    </>
  );
}
