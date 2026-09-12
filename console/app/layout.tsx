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
    // CopilotKit v2's stylesheet ships a full dark palette behind a `.dark`
    // class and defaults to light. Without this the chat renders correctly but
    // almost invisibly — light text on a light surface over our dark shell,
    // which looks like a broken panel rather than a theming problem.
    <html lang="en" className="dark">
      <body>{<Providers>{children}</Providers>}</body>
    </html>
  );
}
