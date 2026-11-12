import type { Metadata } from "next";
import { headers } from "next/headers";
import "./globals.css";
import { Providers } from "@/components/providers";

export const metadata: Metadata = {
  title: { default: "Weakless Automation Engine", template: "%s - Weakless Automation Engine" },
  description: "AI automation infrastructure for business processes.",
  robots: { index: false, follow: false },
};

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Reading the request headers renders every page per request, so Next.js can stamp its
  // scripts with the Content-Security-Policy nonce from middleware.ts (static pages have none).
  await headers();
  return (
    <html lang="en">
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
