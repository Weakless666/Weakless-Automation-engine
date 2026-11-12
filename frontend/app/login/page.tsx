"use client";

import Link from "next/link";
import { Suspense, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { AuthCard } from "@/components/auth-card";
import { MfaChallenge, MfaSetup } from "@/components/mfa";
import { Button, Field, InlineError, Input, Notice } from "@/components/ui";
import { useFinishSignIn } from "@/lib/sign-in";

type Step = { kind: "password" } | { kind: "code"; token: string; email: string } | { kind: "setup"; token: string; email: string };

function LoginForm() {
  const params = useSearchParams();
  const finish = useFinishSignIn();
  const [step, setStep] = useState<Step>({ kind: "password" });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await api<any>("/auth/login", { method: "POST", body: { email, password }, org: false });
      if (res.mfa_required) setStep({ kind: "code", token: res.mfa_token, email: res.email });
      else if (res.mfa_setup_required) setStep({ kind: "setup", token: res.mfa_token, email: res.email });
      else return finish(res);
      setPassword("");
    } catch (err) {
      setError(err);
    }
    setBusy(false);
  }

  const restart = () => { setStep({ kind: "password" }); setError(null); };

  if (step.kind === "code") {
    return (
      <AuthCard title="Two-factor sign-in" description="Enter the code from your authenticator app.">
        <MfaChallenge token={step.token} email={step.email} onDone={finish} onRestart={restart} />
      </AuthCard>
    );
  }
  if (step.kind === "setup") {
    return (
      <AuthCard title="Set up two-factor sign-in" description="Weakless team accounts can reach every client, so they need a second step at sign-in.">
        <MfaSetup
          start={() => api("/auth/mfa/setup", { method: "POST", org: false, body: { mfa_token: step.token } })}
          confirm={(code) => api("/auth/mfa/setup/confirm", { method: "POST", org: false, body: { mfa_token: step.token, code } })}
          onDone={finish}
        />
        <button type="button" onClick={restart} className="mt-4 block w-full text-center text-xs text-muted hover:text-fg">Start over</button>
      </AuthCard>
    );
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
