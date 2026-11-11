"use client";

import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { AuthCard } from "@/components/auth-card";
import { Button, Field, InlineError, Input, Notice } from "@/components/ui";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [done, setDone] = useState<{ email_delivery: boolean } | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setDone(await api("/auth/password-reset/request", { method: "POST", body: { email }, org: false }));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthCard title="Reset your password" description="Enter your account email and we will send you a link to choose a new password.">
      {done ? (
        <div className="space-y-4">
          {done.email_delivery ? (
            <Notice tone="success">If an account exists for {email}, a reset link is on its way. It works once and expires in one hour.</Notice>
          ) : (
            <Notice tone="warning">Email is not set up on this platform yet, so no link could be sent. Ask your Weakless administrator for a password reset link.</Notice>
          )}
          <Link href="/login" className="block text-center text-sm text-primary hover:underline">Back to sign in</Link>
        </div>
      ) : (
        <form onSubmit={submit} className="space-y-4" noValidate>
          <Field label="Email"><Input type="email" autoComplete="username" autoFocus required value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
          <InlineError error={error} />
          <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={!email.includes("@")}>Send reset link</Button>
          <Link href="/login" className="block text-center text-sm text-muted hover:text-fg">Back to sign in</Link>
        </form>
      )}
    </AuthCard>
  );
}
