"use client";

import { useEffect, useState } from "react";
import clsx from "clsx";
import { FileWarning } from "lucide-react";
import { apiBlob } from "@/lib/api";

export type FileRef = { file_id: string; name: string; content_type: string; size: number };

/** An image or video from the organization's files, fetched with the session and shown from memory. */
export function MediaPreview({ file, className, controls = true }: { file: FileRef; className?: string; controls?: boolean }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let alive = true;
    let made: string | null = null;
    setUrl(null); setFailed(false);
    apiBlob(`/files/${file.file_id}/content`, file.content_type)
      .then((blob) => { if (!alive) return; made = URL.createObjectURL(blob); setUrl(made); })
      .catch(() => alive && setFailed(true));
    return () => { alive = false; if (made) URL.revokeObjectURL(made); };
  }, [file.file_id, file.content_type]);
  const box = clsx("flex items-center justify-center overflow-hidden rounded-md bg-raised", className);
  if (failed) return <div className={box} role="img" aria-label={`${file.name} could not be loaded`}><FileWarning className="h-5 w-5 text-muted" /></div>;
  if (!url) return <div className={clsx(box, "animate-pulse")} aria-busy="true" aria-label={`Loading ${file.name}`} />;
  return file.content_type.startsWith("video/")
    ? <video src={url} controls={controls} muted playsInline preload="metadata" aria-label={file.name} className={clsx(box, "object-contain")} />
    // eslint-disable-next-line @next/next/no-img-element -- a blob from the API, not an optimisable asset
    : <img src={url} alt={file.name} className={clsx(box, "object-cover")} />;
}
