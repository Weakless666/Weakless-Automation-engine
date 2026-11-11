"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { api, setCsrfToken } from "@/lib/api";
import { AuthCard } from "@/components/auth-card";
import { Button, Field, InlineError, Input, Notice } from "@/components/ui";

function LoginForm() {
  const router = useRouter();
  const qc = useQueryClient();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<{ csrf_token: string }>("/auth/login", { method: "POST", body: { email, password }, org: false });
      setCsrfToken(res.csrf_token);
      qc.clear();
      router.replace("/");
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Sign in" description="Use the account your Weakless administrator created for you.">
      <form onSubmit={submit} className="space-y-4" noValidate>
        {params.get("expired") && <Notice tone="warning">Your session expired. Sign in again to continue.</Notice>}
        {params.get("reset") && <Notice tone="success">Your password was changed. Sign in with the new password.</Notice>}
        <Field label="Email">
          <Input type="email" autoComplete="username" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        <div>
          <Field label="Password">
            <Input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <Link href="/forgot-password" className="mt-1.5 inline-block text-xs text-muted hover:text-fg">Forgot your password?</Link>
        </div>
        <InlineError error={error} />
        <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={!email || !password}>Sign in</Button>
      </form>
    </AuthCard>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
