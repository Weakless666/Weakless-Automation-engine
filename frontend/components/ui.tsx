"use client";

import { forwardRef, useEffect, useId, useRef } from "react";
import clsx from "clsx";
import { AlertTriangle, Inbox, Loader2, X } from "lucide-react";
import { errorMessage } from "@/lib/api";

/* ------------------------------------------------------------------ primitives */

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  loading?: boolean;
};

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, className, children, disabled, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-md font-medium whitespace-nowrap transition-colors disabled:opacity-50 disabled:cursor-not-allowed",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-9 px-3.5 text-sm",
        variant === "primary" && "bg-primary text-primary-fg hover:bg-primary/90",
        variant === "secondary" && "bg-raised text-fg border hover:bg-line/60",
        variant === "ghost" && "text-muted hover:text-fg hover:bg-raised",
        variant === "danger" && "bg-danger/10 text-danger border border-danger/30 hover:bg-danger/20",
        className,
      )}
      {...rest}
    >
      {loading && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />}
      {children}
    </button>
  );
});

const field = "w-full rounded-md border bg-bg px-3 text-sm text-fg placeholder:text-muted/60 disabled:opacity-60";

export const Input = forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...rest }, ref) {
  return <input ref={ref} className={clsx(field, "h-9", className)} {...rest} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea({ className, ...rest }, ref) {
  return <textarea ref={ref} className={clsx(field, "py-2 leading-relaxed min-h-[84px]", className)} {...rest} />;
});

export const Select = forwardRef<HTMLSelectElement, React.SelectHTMLAttributes<HTMLSelectElement>>(function Select({ className, children, ...rest }, ref) {
  return (
    <select ref={ref} className={clsx(field, "h-9 pr-8", className)} {...rest}>
      {children}
    </select>
  );
});

export function Checkbox({ label, checked, onChange, disabled }: { label: React.ReactNode; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className={clsx("inline-flex items-center gap-2 text-sm", disabled ? "opacity-60" : "cursor-pointer")}>
      <input type="checkbox" className="h-4 w-4 accent-[rgb(var(--primary))]" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

/**
 * Form field with caption, hint and error. Wraps a single input in a <label>. Pass `group` for
 * compound controls (lists, repeated items, several inputs): a <label> around buttons would make
 * the caption press the first button and would give every inner control the caption as its name.
 */
export function Field({ label, hint, error, children, required, group }: {
  label: string; hint?: React.ReactNode; error?: string | null; children: React.ReactNode; required?: boolean; group?: boolean;
}) {
  const id = useId();
  const caption = (
    <span id={id} className="mb-1 flex items-center gap-1 text-xs font-medium text-muted">
      {label}
      {required && <span className="text-primary" aria-hidden>*</span>}
    </span>
  );
  const note = error ? <span className="mt-1 block text-xs text-danger">{error}</span> : hint ? <span className="mt-1 block text-xs text-muted/80">{hint}</span> : null;
  if (group) {
    return <div role="group" aria-labelledby={id} className="block">{caption}{children}{note}</div>;
  }
  return <label className="block">{caption}{children}{note}</label>;
}

/* ------------------------------------------------------------------ layout */

export function PageHeader({ title, description, actions }: { title: string; description?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 max-w-3xl text-sm text-muted">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Card({ className, children, ...rest }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={clsx("rounded-lg border bg-surface", className)} {...rest}>
      {children}
    </div>
  );
}

export function CardHeader({ title, description, actions }: { title: string; description?: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3 border-b px-4 py-3">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

export function Stat({ label, value, sub, tone }: { label: string; value: React.ReactNode; sub?: React.ReactNode; tone?: "danger" | "success" | "warning" }) {
  return (
    <Card className="px-4 py-3.5">
      <div className="text-xs text-muted">{label}</div>
      <div className={clsx("tabular mt-1 text-2xl font-semibold tracking-tight", tone === "danger" && "text-danger", tone === "success" && "text-success", tone === "warning" && "text-warning")}>{value}</div>
      {sub && <div className="mt-0.5 text-xs text-muted">{sub}</div>}
    </Card>
  );
}

/* ------------------------------------------------------------------ feedback */

const TONES: Record<string, string> = {
  neutral: "bg-raised text-muted border-line",
  success: "bg-success/10 text-success border-success/25",
  danger: "bg-danger/10 text-danger border-danger/25",
  warning: "bg-warning/10 text-warning border-warning/25",
  info: "bg-info/10 text-info border-info/25",
  primary: "bg-primary/10 text-primary border-primary/30",
};

export function Badge({ tone = "neutral", children, className }: { tone?: keyof typeof TONES | string; children: React.ReactNode; className?: string }) {
  return <span className={clsx("inline-flex items-center gap-1 rounded-sm border px-1.5 py-0.5 text-2xs font-medium uppercase tracking-wide", TONES[tone] ?? TONES.neutral, className)}>{children}</span>;
}

const STATUS_TONE: Record<string, string> = {
  COMPLETED: "success", connected: "success", active: "success", APPROVED: "success", sent: "success", processed: "success", published: "success", installed: "success",
  FAILED: "danger", error: "danger", REJECTED: "danger", failed: "danger", rejected: "danger", suspended: "danger", past_due: "danger", DEAD: "danger", canceled: "danger",
  RUNNING: "info", QUEUED: "info", PENDING: "info", RETRYING: "warning", WAITING: "warning", WAITING_APPROVAL: "warning", pending: "warning",
  awaiting_credentials: "warning", invited: "info", duplicate: "neutral", ignored: "neutral", draft: "neutral", CANCELLED: "neutral", revoked: "neutral", EXPIRED: "neutral",
  inactive: "neutral", disabled: "neutral", trialing: "info", onboarding: "info", lead: "info", paused: "warning", churned: "neutral", archived: "neutral", deprecated: "neutral", received: "info",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return null;
  return <Badge tone={STATUS_TONE[status] ?? "neutral"}>{status.replace(/_/g, " ")}</Badge>;
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted" role="status">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> {label}...
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-danger/25 bg-danger/5 px-6 py-10 text-center" role="alert">
      <AlertTriangle className="h-5 w-5 text-danger" aria-hidden />
      <p className="text-sm">{errorMessage(error)}</p>
      {onRetry && <Button size="sm" onClick={onRetry}>Try again</Button>}
    </div>
  );
}

export function InlineError({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="rounded-md border border-danger/25 bg-danger/5 px-3 py-2 text-xs text-danger" role="alert">{errorMessage(error)}</p>;
}

export function Notice({ tone = "info", children }: { tone?: "info" | "warning" | "success"; children: React.ReactNode }) {
  return <div className={clsx("rounded-md border px-3 py-2 text-xs leading-relaxed", TONES[tone])}>{children}</div>;
}

export function EmptyState({ title, description, action, icon }: { title: string; description?: React.ReactNode; action?: React.ReactNode; icon?: React.ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-14 text-center">
      <div className="mb-1 flex h-10 w-10 items-center justify-center rounded-lg border bg-raised text-muted">{icon ?? <Inbox className="h-5 w-5" aria-hidden />}</div>
      <p className="text-sm font-medium">{title}</p>
      {description && <p className="max-w-md text-sm text-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/* ------------------------------------------------------------------ table */

export function Table({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <div className="overflow-x-auto">
      <table className={clsx("w-full text-left text-sm", className)}>{children}</table>
    </div>
  );
}
export function Th({ children, className }: { children?: React.ReactNode; className?: string }) {
  return <th className={clsx("border-b px-4 py-2 text-2xs font-medium uppercase tracking-wider text-muted", className)}>{children}</th>;
}
export function Td({ children, className, ...rest }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={clsx("border-b border-line/60 px-4 py-2.5 align-middle", className)} {...rest}>
      {children}
    </td>
  );
}

export function Pager({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (offset: number) => void }) {
  if (total <= limit) return null;
  const from = offset + 1;
  const to = Math.min(total, offset + limit);
  return (
    <div className="flex items-center justify-between px-4 py-2.5 text-xs text-muted">
      <span className="tabular">{from}-{to} of {total}</span>
      <div className="flex gap-2">
        <Button size="sm" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>Previous</Button>
        <Button size="sm" disabled={to >= total} onClick={() => onChange(offset + limit)}>Next</Button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ dialog & tabs */

export function Dialog({ open, onClose, title, description, children, footer, wide }: {
  open: boolean; onClose: () => void; title: string; description?: React.ReactNode; children: React.ReactNode; footer?: React.ReactNode; wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (open && !el.open) el.showModal();
    if (!open && el.open) el.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      onClose={onClose}
      onClick={(e) => e.target === ref.current && onClose()}
      className={clsx("m-auto w-[calc(100vw-2rem)] rounded-lg border bg-surface p-0 text-fg backdrop:bg-black/60", wide ? "max-w-3xl" : "max-w-lg")}
    >
      {open && (
        <div className="flex max-h-[85vh] flex-col">
          <div className="flex items-start justify-between gap-4 border-b px-5 py-4">
            <div>
              <h2 id={titleId} className="text-base font-semibold">{title}</h2>
              {description && <p className="mt-1 text-xs text-muted">{description}</p>}
            </div>
            <button type="button" onClick={onClose} aria-label="Close" className="rounded-md p-1 text-muted hover:bg-raised hover:text-fg">
              <X className="h-4 w-4" />
            </button>
          </div>
          <div className="space-y-4 overflow-y-auto px-5 py-4">{children}</div>
          {footer && <div className="flex justify-end gap-2 border-t px-5 py-3">{footer}</div>}
        </div>
      )}
    </dialog>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { key: T; label: string; count?: number }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="mb-4 flex gap-1 border-b" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.key}
          role="tab"
          type="button"
          aria-selected={value === t.key}
          onClick={() => onChange(t.key)}
          className={clsx("-mb-px border-b-2 px-3 py-2 text-sm transition-colors", value === t.key ? "border-primary text-fg" : "border-transparent text-muted hover:text-fg")}
        >
          {t.label}
          {t.count !== undefined && <span className="tabular ml-1.5 rounded-sm bg-raised px-1.5 py-0.5 text-2xs text-muted">{t.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function Code({ children, className }: { children: React.ReactNode; className?: string }) {
  return <pre className={clsx("max-h-80 overflow-auto rounded-md border bg-bg p-3 font-mono text-xs leading-relaxed text-fg/90", className)}>{children}</pre>;
}

export function Json({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-xs text-muted">-</span>;
  return <Code>{JSON.stringify(value, null, 2)}</Code>;
}

export function NeedOrg({ what }: { what: string }) {
  return <EmptyState title="Select an organization" description={`${what} belong to an organization. Choose one in the switcher at the top of the sidebar.`} />;
}
