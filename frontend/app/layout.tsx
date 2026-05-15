import "./globals.css";
import type { ReactNode } from "react";
import { Providers } from "./providers";
import { dict } from "@/lib/i18n";
import { ShellHeader } from "@/components/ShellHeader";

export const metadata = {
  title: dict.app_title,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-Hant-TW">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link rel="preconnect" href="https://fonts.gstatic.com" crossOrigin="" />
        <link
          rel="stylesheet"
          href="https://fonts.googleapis.com/css2?family=Noto+Sans+TC:wght@300;400;500;600&family=Noto+Serif+TC:wght@400;500;700&display=swap"
        />
      </head>
      <body>
        <Providers>
          <div className="shell">
            <ShellHeader />
            <main className="shell-main">{children}</main>
          </div>
        </Providers>
      </body>
    </html>
  );
}
