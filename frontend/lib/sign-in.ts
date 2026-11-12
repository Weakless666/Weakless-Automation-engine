"use client";

import { useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { setCsrfToken } from "./api";

/** Once the server has started a session: remember its CSRF token and enter the app fresh. */
export function useFinishSignIn() {
  const router = useRouter();
  const qc = useQueryClient();
  return (res: { csrf_token?: string }) => {
    if (res?.csrf_token) setCsrfToken(res.csrf_token);
    qc.clear();
    router.replace("/");
  };
}
