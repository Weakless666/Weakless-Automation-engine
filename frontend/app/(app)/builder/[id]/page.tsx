"use client";

import { use } from "react";
import { Builder } from "@/components/builder/builder";

export default function BuilderPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  return <Builder workflowId={id} />;
}
