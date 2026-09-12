import type { Metadata } from "next";
import "@copilotkit/react-core/v2/styles.css";
import "./globals.css";
import { Providers } from "@/components/Providers";

export const metadata: Metadata = {
  title: "Shum-AI — operator console",
  description:
    "Human-in-the-loop gate on a real outbound restaurant phone call, built with CopilotKit v2.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{<Providers>{children}</Providers>}</body>
    </html>
  );
}
