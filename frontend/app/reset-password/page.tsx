"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { AuthCard, passwordProblem } from "@/components/auth-card";
import { Button, Field, InlineError, Input, Notice } from "@/components/ui";

function ResetForm() {
  const router = useRouter();
  const token = useSearchParams().get("token") ?? "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const problem = passwordProblem(password, confirm);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (problem) return;
    setBusy(true);
    setError(null);
    try {
      await api("/auth/password-reset/confirm", { method: "POST", body: { token, new_password: password }, org: false });
      router.replace("/login?reset=1");
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  if (!token) {
    return (
      <AuthCard title="Reset link missing">
        <Notice tone="warning">This page needs the link from your reset email.</Notice>
        <Link href="/forgot-password" className="mt-4 block text-center text-sm text-primary hover:underline">Request a new link</Link>
      </AuthCard>
    );
  }
  return (
    <AuthCard title="Choose a new password" description="This signs you out everywhere else.">
      <form onSubmit={submit} className="space-y-4" noValidate>
        <Field label="New password" hint="At least 10 characters, with an uppercase letter and a digit.">
          <Input type="password" autoComplete="new-password" autoFocus value={password} onChange={(e) => setPassword(e.target.value)} />
        </Field>
        <Field label="Repeat new password" error={touched && problem ? problem : null}>
          <Input type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        </Field>
        <InlineError error={error} />
        <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={!password || !confirm}>Set new password</Button>
        {error ? <Link href="/forgot-password" className="block text-center text-sm text-primary hover:underline">Request a new link</Link> : null}
      </form>
    </AuthCard>
  );
}

export default function ResetPasswordPage() {
  return <Suspense><ResetForm /></Suspense>;
}
