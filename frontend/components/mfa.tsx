"use client";

import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { Check, Copy, KeyRound } from "lucide-react";
import { api } from "@/lib/api";
import { Button, Field, InlineError, Input, Notice, Spinner } from "./ui";

/** Second sign-in step: a code from the authenticator app, or a recovery code. */
export function MfaChallenge({ token, email, onDone, onRestart }: { token: string; email: string; onDone: (res: any) => void; onRestart: () => void }) {
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onDone(await api("/auth/mfa/verify", { method: "POST", org: false, body: { mfa_token: token, code } }));
    } catch (err) {
      setError(err);
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      <p className="text-sm text-muted">Signing in as <span className="text-fg">{email}</span>.</p>
      {recovery ? (
        <Field label="Recovery code" hint="One of the codes you saved when you set up two-factor sign-in. Each works once.">
          <Input value={code} onChange={(e) => setCode(e.target.value)} autoFocus autoComplete="off" spellCheck={false} placeholder="xxxxx-xxxxx" className="font-mono" />
        </Field>
      ) : (
        <Field label="Authentication code" hint="The 6-digit code from your authenticator app.">
          <Input value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} autoFocus inputMode="numeric" autoComplete="one-time-code" placeholder="123456" className="font-mono tracking-widest" />
        </Field>
      )}
      <InlineError error={error} />
      <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={recovery ? code.trim().length < 10 : code.length !== 6}>Verify</Button>
      <div className="flex justify-between text-xs">
        <button type="button" className="text-muted hover:text-fg" onClick={() => { setRecovery(!recovery); setCode(""); setError(null); }}>
          {recovery ? "Use the authenticator app" : "Use a recovery code"}
        </button>
        <button type="button" className="text-muted hover:text-fg" onClick={onRestart}>Start over</button>
      </div>
    </form>
  );
}

/** Scan, confirm with a first code, then save the recovery codes. Used at sign-in and in Settings. */
export function MfaSetup({ start, confirm, onDone, intro }: {
  start: () => Promise<{ secret: string; otpauth_url: string }>;
  confirm: (code: string) => Promise<any>;
  onDone: (result: any) => void;
  intro?: React.ReactNode;
}) {
  const [setup, setSetup] = useState<{ secret: string; otpauth_url: string; qr: string } | null>(null);
  const [startError, setStartError] = useState<unknown>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [result, setResult] = useState<any>(null);

  useEffect(() => {
    let live = true;
    start()
      .then(async (s) => { const qr = await QRCode.toDataURL(s.otpauth_url, { margin: 1, width: 192 }); if (live) setSetup({ ...s, qr }); })
      .catch((e) => live && setStartError(e));
    return () => { live = false; };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(await confirm(code));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  if (result) return <RecoveryCodes codes={result.recovery_codes} onContinue={() => onDone(result)} />;
  if (startError) return <InlineError error={startError} />;
  if (!setup) return <Spinner label="Preparing" />;
  return (
    <form onSubmit={submit} className="space-y-4" noValidate>
      {intro}
      <ol className="list-decimal space-y-1 pl-5 text-sm text-muted">
        <li>Open an authenticator app on your phone (Google Authenticator, Microsoft Authenticator, 1Password...).</li>
        <li>Scan this code, or enter the key by hand.</li>
        <li>Type the 6-digit code the app shows.</li>
      </ol>
      <div className="flex items-center gap-4">
        <img src={setup.qr} alt="QR code for your authenticator app" width={152} height={152} className="rounded-md bg-white p-1.5" />
        <div className="min-w-0 text-xs">
          <div className="mb-1 text-muted">Key for manual entry</div>
          <div className="break-all font-mono text-sm" aria-label="Setup key">{setup.secret.match(/.{1,4}/g)?.join(" ")}</div>
        </div>
      </div>
      <Field label="Code from the app">
        <Input value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" placeholder="123456" className="font-mono tracking-widest" />
      </Field>
      <InlineError error={error} />
      <Button type="submit" variant="primary" className="w-full" loading={busy} disabled={code.length !== 6}>Turn on two-factor sign-in</Button>
    </form>
  );
}

export function RecoveryCodes({ codes, onContinue, continueLabel = "I saved these codes" }: { codes: string[]; onContinue: () => void; continueLabel?: string }) {
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);
  const text = codes.join("\n");
  return (
    <div className="space-y-4">
      <Notice tone="success">Two-factor sign-in is on.</Notice>
      <div>
        <div className="mb-1 flex items-center gap-1.5 text-sm font-medium"><KeyRound className="h-4 w-4 text-primary" aria-hidden /> Recovery codes</div>
        <p className="text-xs text-muted">If you lose your phone, each of these lets you sign in once. Store them somewhere safe, such as a password manager. They are shown only now.</p>
      </div>
      <ul className="grid grid-cols-2 gap-1.5 rounded-md border bg-bg p-3 font-mono text-sm" aria-label="Recovery codes">
        {codes.map((c) => <li key={c}>{c}</li>)}
      </ul>
      <div className="flex gap-2">
        <Button size="sm" onClick={async () => { try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* select the list instead */ } }}>
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />} Copy
        </Button>
        <Button size="sm" onClick={() => {
          const url = URL.createObjectURL(new Blob([`Weakless recovery codes\n\n${text}\n`], { type: "text/plain" }));
          const a = Object.assign(document.createElement("a"), { href: url, download: "weakless-recovery-codes.txt" });
          a.click();
          URL.revokeObjectURL(url);
        }}>Download</Button>
      </div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" className="h-4 w-4 accent-[rgb(var(--primary))]" checked={saved} onChange={(e) => setSaved(e.target.checked)} /> I have stored my recovery codes</label>
      <Button variant="primary" className="w-full" disabled={!saved} onClick={onContinue}>{continueLabel}</Button>
    </div>
  );
}
