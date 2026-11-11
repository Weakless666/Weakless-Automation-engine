"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { api, setCsrfToken } from "@/lib/api";
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
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm">
        <div className="mb-8 flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-fg">W</div>
          <div className="leading-tight">
            <div className="text-base font-semibold">Weakless</div>
            <div className="text-2xs uppercase tracking-wider text-muted">Automation Engine</div>
          </div>
        </div>
        <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
        <p className="mt-1 text-sm text-muted">Use the account your Weakless administrator created for you.</p>
        <form onSubmit={submit} className="mt-6 space-y-4" noValidate>
          {params.get("expired") && <Notice tone="warning">Your session expired. Sign in again to continue.</Notice>}
          <Field label="Email">
            <Input type="email" autoComplete="username" required autoFocus value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label="Password">
            <Input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          <InlineError error={error} />
          <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={!email || !password}>Sign in</Button>
        </form>
      </div>
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
