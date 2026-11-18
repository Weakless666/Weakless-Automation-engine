"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import {
  Activity, BarChart3, Bell, Bot, Building2, CalendarDays, FolderOpen, Sparkles, Users, CreditCard, LayoutDashboard, LayoutTemplate, LogOut, Menu, Package, PencilRuler, Plug, ScrollText,
  Settings, Workflow, X,
} from "lucide-react";
import { api } from "@/lib/api";
import { useSession } from "@/lib/session";
import { titleCase } from "@/lib/format";
import { Select } from "./ui";

type Item = { href: string; label: string; icon: React.ComponentType<{ className?: string }>; show: (s: ReturnType<typeof useSession>) => boolean };

// One navigation model; each role sees the subset it is authorised for.
const NAV: Item[] = [
  { href: "/", label: "Overview", icon: LayoutDashboard, show: (s) => s.can("analytics.read") || s.can("platform.analytics.read") },
  { href: "/organizations", label: "Organizations", icon: Building2, show: (s) => s.isStaff || s.can("org.read") },
  { href: "/automations", label: "Automations", icon: Workflow, show: (s) => s.can("workflows.read") },
  { href: "/builder", label: "Workflow Builder", icon: PencilRuler, show: (s) => s.can("workflows.write") },
  { href: "/agents", label: "AI Agents", icon: Bot, show: (s) => s.can("agents.read") },
  { href: "/teams", label: "AI Teams", icon: Users, show: (s) => s.can("agents.read") },
  { href: "/content", label: "Content", icon: CalendarDays, show: (s) => s.can("content.read") },
  { href: "/studio", label: "Creative Studio", icon: Sparkles, show: (s) => s.can("content.read") },
  { href: "/files", label: "Files", icon: FolderOpen, show: (s) => s.can("files.read") },
  { href: "/integrations", label: "Integrations", icon: Plug, show: (s) => s.can("integrations.read") },
  { href: "/templates", label: "Templates", icon: LayoutTemplate, show: (s) => s.can("templates.read") },
  { href: "/solutions", label: "Industry Solutions", icon: Package, show: (s) => s.can("templates.read") },
  { href: "/executions", label: "Executions", icon: Activity, show: (s) => s.can("executions.read") },
  { href: "/analytics", label: "Analytics", icon: BarChart3, show: (s) => s.can("analytics.read") || s.can("platform.analytics.read") },
  { href: "/billing", label: "Usage & Billing", icon: CreditCard, show: (s) => s.can("billing.read") },
  { href: "/notifications", label: "Notifications", icon: Bell, show: () => true },
  { href: "/audit", label: "Audit Logs", icon: ScrollText, show: (s) => s.can("audit.read") || s.can("platform.audit.read") },
  { href: "/settings", label: "Settings", icon: Settings, show: () => true },
];

export function Shell({ children }: { children: React.ReactNode }) {
  const session = useSession();
  const pathname = usePathname();
  const unread = useQuery({ queryKey: ["notifications", "unread"], queryFn: () => api<{ unread: number }>("/notifications", { params: { limit: 1, unread: true }, org: false }), refetchInterval: 30_000 });
  const items = NAV.filter((i) => i.show(session));
  const isBuilder = pathname.startsWith("/builder/");
  // Below the lg breakpoint the sidebar is a drawer. It is the same element as on desktop,
  // so there is one navigation and one organization switcher in the page at any size.
  const [open, setOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const drawer = useRef<HTMLElement>(null);
  useEffect(() => setOpen(false), [pathname, session.org?.id]);
  useEffect(() => {
    if (!open) return;
    drawer.current?.querySelector<HTMLElement>("a, select, button")?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); menuButton.current?.focus(); } };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);
  const current = items.find((i) => (i.href === "/" ? pathname === "/" : pathname === i.href || pathname.startsWith(i.href + "/")));
  const unreadCount = unread.data?.unread ?? 0;

  return (
    <div className="flex h-dvh overflow-hidden max-lg:flex-col">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b bg-surface px-2 lg:hidden">
        <button ref={menuButton} type="button" onClick={() => setOpen(true)} aria-label="Open menu" aria-expanded={open} aria-controls="app-sidebar"
          className="relative rounded-md p-2 text-muted hover:bg-raised hover:text-fg">
          <Menu className="h-5 w-5" />
          {unreadCount > 0 && <span className="absolute right-1 top-1 h-2 w-2 rounded-full bg-primary" aria-hidden />}
        </button>
        <div className="flex h-6 w-6 items-center justify-center rounded-md bg-primary text-2xs font-bold text-primary-fg">W</div>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{current?.label ?? "Weakless"}</span>
        {session.org && <span className="max-w-[40%] truncate text-xs text-muted">{session.org.name}</span>}
      </div>
      {open && <div className="fixed inset-0 z-40 bg-black/60 lg:hidden" onClick={() => setOpen(false)} aria-hidden />}
      <aside id="app-sidebar" ref={drawer} aria-label="Sidebar"
        className={clsx("flex w-60 shrink-0 flex-col border-r bg-surface",
          open ? "max-lg:fixed max-lg:inset-y-0 max-lg:left-0 max-lg:z-50 max-lg:w-72 max-lg:max-w-[85vw] max-lg:shadow-2xl" : "max-lg:hidden")}>
        <div className="flex h-14 items-center gap-2.5 border-b px-4">
          <div className="flex h-7 w-7 items-center justify-center rounded-md bg-primary text-xs font-bold text-primary-fg">W</div>
          <div className="flex-1 leading-tight">
            <div className="text-sm font-semibold">Weakless</div>
            <div className="text-2xs uppercase tracking-wider text-muted">Automation Engine</div>
          </div>
          <button type="button" onClick={() => { setOpen(false); menuButton.current?.focus(); }} aria-label="Close menu"
            className="rounded-md p-1.5 text-muted hover:bg-raised hover:text-fg lg:hidden"><X className="h-4 w-4" /></button>
        </div>

        <div className="border-b px-3 py-3">
          <label className="mb-1 block text-2xs font-medium uppercase tracking-wider text-muted" htmlFor="org-switcher">Organization</label>
          {session.me.organizations.length === 0 ? (
            <p className="text-xs text-muted">{session.isStaff ? "No organizations yet." : "You are not a member of any organization."}</p>
          ) : (
            <Select id="org-switcher" value={session.org?.id ?? ""} onChange={(e) => session.selectOrg(e.target.value || null)}>
              {session.isStaff && <option value="">All organizations</option>}
              {session.me.organizations.map((o) => (
                <option key={o.id} value={o.id}>{o.name}</option>
              ))}
            </Select>
          )}
        </div>

        <nav className="flex-1 overflow-y-auto px-2 py-2" aria-label="Main">
          {items.map((item) => {
            const active = item.href === "/" ? pathname === "/" : pathname === item.href || pathname.startsWith(item.href + "/");
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={clsx("group mb-0.5 flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-sm transition-colors",
                  active ? "bg-primary/10 text-fg" : "text-muted hover:bg-raised hover:text-fg")}
              >
                <Icon className={clsx("h-4 w-4 shrink-0", active ? "text-primary" : "text-muted group-hover:text-fg")} />
                <span className="flex-1 truncate">{item.label}</span>
                {item.href === "/notifications" && unreadCount > 0 && (
                  <span className="tabular rounded-sm bg-primary px-1.5 text-2xs font-semibold text-primary-fg">{unread.data!.unread > 99 ? "99+" : unread.data!.unread}</span>
                )}
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-2 border-t px-3 py-3">
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm">{session.me.user.full_name || session.me.user.email}</div>
            <div className="truncate text-2xs uppercase tracking-wider text-muted">{titleCase((session.org?.role ?? session.me.user.platform_role ?? "member").toLowerCase())}</div>
          </div>
          <button type="button" onClick={session.logout} title="Sign out" aria-label="Sign out" className="rounded-md p-1.5 text-muted hover:bg-raised hover:text-fg">
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </aside>

      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
        {isBuilder ? children : <div className="mx-auto max-w-[1280px] px-4 py-5 sm:px-6 sm:py-6 lg:px-8">{children}</div>}
      </main>
    </div>
  );
}
