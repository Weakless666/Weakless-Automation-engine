"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PencilRuler, Plus } from "lucide-react";
import { api, type Page } from "@/lib/api";
import { useOrgKey, useSession } from "@/lib/session";
import { fmtRelative } from "@/lib/format";
import { Badge, Button, Card, EmptyState, ErrorState, NeedOrg, PageHeader, Spinner } from "@/components/ui";
import { NewWorkflowDialog } from "@/components/new-workflow";

export default function BuilderIndex() {
  const { org } = useSession();
  const key = useOrgKey();
  const [creating, setCreating] = useState(false);
  const list = useQuery({ queryKey: [key, "workflows", "builder"], queryFn: () => api<Page<any>>("/workflows", { params: { limit: 100 } }), enabled: !!org });
  if (!org) return <><PageHeader title="Workflow Builder" /><NeedOrg what="Workflows" /></>;
  return (
    <>
      <PageHeader title="Workflow Builder" description="Open a workflow to edit its graph, test it, and publish a new version."
        actions={<Button variant="primary" onClick={() => setCreating(true)}><Plus className="h-4 w-4" /> New workflow</Button>} />
      {list.isLoading ? <Spinner /> : list.error ? <ErrorState error={list.error} onRetry={() => list.refetch()} /> : list.data!.items.length === 0 ? (
        <Card><EmptyState icon={<PencilRuler className="h-5 w-5" />} title="Nothing to edit yet" description="Create a workflow to open the visual builder."
          action={<Button variant="primary" onClick={() => setCreating(true)}>New workflow</Button>} /></Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {list.data!.items.map((w) => (
            <Link key={w.id} href={`/builder/${w.id}`} className="rounded-lg border bg-surface p-4 transition-colors hover:border-primary/50">
              <div className="flex items-start justify-between gap-2">
                <div className="font-medium">{w.name}</div>
                {w.is_active ? <Badge tone="success">Active</Badge> : w.status === "published" ? <Badge>Inactive</Badge> : <Badge tone="warning">Draft</Badge>}
              </div>
              <p className="mt-1 line-clamp-2 min-h-[2rem] text-xs text-muted">{w.description || "No description"}</p>
              <div className="mt-3 text-2xs uppercase tracking-wider text-muted">{w.version ? `Published v${w.version}` : "Never published"} - edited {fmtRelative(w.updated_at)}</div>
            </Link>
          ))}
        </div>
      )}
      <NewWorkflowDialog open={creating} onClose={() => setCreating(false)} />
    </>
  );
}
