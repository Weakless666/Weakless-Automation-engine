"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, setCsrfToken } from "@/lib/api";
import { fmtDate, titleCase } from "@/lib/format";
import { AuthCard, passwordProblem } from "@/components/auth-card";
import { Button, ErrorState, Field, InlineError, Input, Notice, Spinner } from "@/components/ui";

type Info = { email: string; full_name: string; organization_name: string | null; role: string | null; invited_by: string | null; needs_password: boolean; expires_at: string };

function AcceptForm() {
  const router = useRouter();
  const qc = useQueryClient();
  const token = useSearchParams().get("token") ?? "";
  const info = useQuery({ queryKey: ["invitation", token], queryFn: () => api<Info>("/auth/invitations/inspect", { method: "POST", body: { token }, org: false }), enabled: !!token, retry: false });
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  if (!token) return <AuthCard title="Invitation link missing"><Notice tone="warning">Open the link from your invitation email.</Notice></AuthCard>;
  if (info.isLoading) return <AuthCard title="Opening invitation"><Spinner /></AuthCard>;
  if (info.error || !info.data) {
    return (
      <AuthCard title="Invitation unavailable">
        <ErrorState error={info.error} />
        <p className="mt-4 text-center text-sm text-muted">Ask the person who invited you to send a new invitation.</p>
        <Link href="/login" className="mt-2 block text-center text-sm text-primary hover:underline">Go to sign in</Link>
      </AuthCard>
    );
  }
  const d = info.data;
  const needs = d.needs_password;
  const problem = needs ? passwordProblem(password, confirm) : password ? null : "Enter your password.";
  const where = d.organization_name ?? "the Weakless team";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (problem) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ csrf_token: string }>("/auth/invitations/accept", { method: "POST", org: false,
        body: { token, password, full_name: needs ? name || d.full_name : null } });
      setCsrfToken(res.csrf_token);
      qc.clear();
      router.replace("/");
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <AuthCard title={`Join ${where}`}
      description={<>{d.invited_by ? `${d.invited_by} invited you` : "You were invited"}{d.role ? ` as ${titleCase(d.role.toLowerCase())}` : ""}. The invitation expires {fmtDate(d.expires_at)}.</>}>
      <form onSubmit={submit} className="space-y-4" noValidate>
        <Field label="Email"><Input value={d.email} disabled /></Field>
        {needs ? (
          <>
            <Field label="Your name"><Input value={name} placeholder={d.full_name} onChange={(e) => setName(e.target.value)} autoComplete="name" /></Field>
            <Field label="Choose a password" hint="At least 10 characters, with an uppercase letter and a digit.">
              <Input type="password" autoComplete="new-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
            </Field>
            <Field label="Repeat password" error={touched && problem ? problem : null}>
              <Input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
            </Field>
          </>
        ) : (
          <>
            <Notice>You already have an account. Enter your current password to join {where}.</Notice>
            <Field label="Password" error={touched && problem ? problem : null}>
              <Input type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} autoFocus />
            </Field>
            <Link href="/forgot-password" className="block text-xs text-muted hover:text-fg">Forgot your password?</Link>
          </>
        )}
        <InlineError error={error} />
        <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={!password}>{needs ? "Create account and join" : "Join"}</Button>
      </form>
    </AuthCard>
  );
}

export default function AcceptInvitePage() {
  return <Suspense><AcceptForm /></Suspense>;
}
