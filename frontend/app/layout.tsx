import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Shovel — local knowledge workbench",
  description: "Connect local documents, manage background tasks, and ask your knowledge.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
