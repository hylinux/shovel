import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./globals.css";
import { LocaleProvider } from "./locale-context";

export const metadata: Metadata = {
  description: "Connect local documents, manage background tasks, and ask your knowledge.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body><LocaleProvider>{children}</LocaleProvider></body>
    </html>
  );
}
