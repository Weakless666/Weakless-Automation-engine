"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useOrgKey } from "@/lib/session";
import { Button, Dialog, Field, InlineError, Input, Textarea } from "./ui";

export function NewWorkflowDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const router = useRouter();
  const qc = useQueryClient();
  const key = useOrgKey();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const create = useMutation({
    mutationFn: () => api("/workflows", { method: "POST", body: { name, description } }),
    onSuccess: (wf: any) => {
      qc.invalidateQueries({ queryKey: [key, "workflows"] });
      onClose();
      router.push(`/builder/${wf.id}`);
    },
  });
  return (
    <Dialog open={open} onClose={onClose} title="New workflow" description="Starts as a draft with a manual trigger. Nothing runs until you publish and activate it."
      footer={<><Button onClick={onClose}>Cancel</Button><Button variant="primary" loading={create.isPending} disabled={!name.trim()} onClick={() => create.mutate()}>Create and open builder</Button></>}>
      <Field label="Name" required><Input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></Field>
      <Field label="Description"><Textarea value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
      <InlineError error={create.error} />
    </Dialog>
  );
}
