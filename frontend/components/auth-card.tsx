"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button, Input, Notice } from "./ui";
import { fmtDate } from "@/lib/format";

/** Centered card used by the public account pages (sign in, reset, invitation). */
export function AuthCard({ title, description, children }: { title: string; description?: React.ReactNode; children: React.ReactNode }) {
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
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted">{description}</p>}
        <div className="mt-6">{children}</div>
      </div>
    </div>
  );
}

export function passwordProblem(password: string, confirm: string): string | null {
  if (password.length < 10) return "Use at least 10 characters.";
  if (password.toLowerCase() === password || !/\d/.test(password)) return "Include an uppercase letter and a digit.";
  if (password !== confirm) return "The passwords do not match.";
  return null;
}

/** Shows a freshly issued invitation or reset link, with copy, and says honestly whether it was emailed. */
export function IssuedLink({ link, kind }: { link: { url: string; expires_at: string; email_sent?: boolean }; kind: "invitation" | "reset" }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-2">
      {link.email_sent ? <Notice tone="success">The {kind === "invitation" ? "invitation" : "link"} was emailed. You can also share the link below.</Notice>
        : <Notice tone="warning">{kind === "invitation" ? "The invitation was not emailed" : "This link was not emailed"} (platform email is not configured). Send the link below to the person over a channel you trust. Anyone who has it can use it.</Notice>}
      <div className="flex gap-1.5">
        <Input readOnly value={link.url} onFocus={(e) => e.target.select()} className="h-8 font-mono text-2xs" aria-label="Link" />
        <Button size="sm" aria-label="Copy link" onClick={async () => { try { await navigator.clipboard.writeText(link.url); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { /* the field is selectable */ } }}>
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
        </Button>
      </div>
      <p className="text-xs text-muted">Works once. Expires {fmtDate(link.expires_at)}.</p>
    </div>
  );
}
