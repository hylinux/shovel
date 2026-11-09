import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./tailwind.css";
import "./globals.css";
import "katex/dist/katex.min.css";
import { LocaleProvider } from "./locale-context";

export const metadata: Metadata = {
  description: "Connect local documents, manage background tasks, and ask your knowledge.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  // Extensions can inject root attributes before hydration; descendants remain checked.
  return (
    <html lang="en" suppressHydrationWarning>
      <body><LocaleProvider>{children}</LocaleProvider></body>
    </html>
  );
}
