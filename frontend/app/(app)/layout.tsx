"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ApiError } from "@/lib/api";
import { SessionProvider, useMe } from "@/lib/session";
import { Shell } from "@/components/shell";
import { ErrorState, Spinner } from "@/components/ui";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const me = useMe();
  const unauthorized = me.error instanceof ApiError && me.error.status === 401;

  useEffect(() => {
    if (unauthorized) router.replace("/login");
  }, [unauthorized, router]);

  useEffect(() => {
    const onExpired = () => router.replace("/login?expired=1");
    window.addEventListener("wae:unauthorized", onExpired);
    return () => window.removeEventListener("wae:unauthorized", onExpired);
  }, [router]);

  if (me.isLoading || unauthorized) return <div className="flex h-screen items-center justify-center"><Spinner label="Loading workspace" /></div>;
  if (me.error || !me.data) return <div className="mx-auto mt-24 max-w-md"><ErrorState error={me.error} onRetry={() => me.refetch()} /></div>;

  return (
    <SessionProvider me={me.data}>
      <Shell>{children}</Shell>
    </SessionProvider>
  );
}
