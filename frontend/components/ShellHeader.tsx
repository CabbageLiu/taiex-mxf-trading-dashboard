"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { dict, t } from "@/lib/i18n";
import { StatusPill } from "./StatusPill";

const NAV: { href: string; key: "nav.trading" | "nav.analysis" }[] = [
  { href: "/trading", key: "nav.trading" },
  { href: "/analysis", key: "nav.analysis" },
];

export function ShellHeader() {
  const pathname = usePathname() ?? "";
  const isActive = (href: string) => pathname === href || pathname.startsWith(`${href}/`);

  return (
    <header className="shell-header">
      <strong className="brand">{dict.app_title}</strong>
      <nav className="shell-nav" aria-label="primary">
        {NAV.map((item) => (
          <Link
            key={item.href}
            href={item.href}
            className={`nav-link${isActive(item.href) ? " active" : ""}`}
            aria-current={isActive(item.href) ? "page" : undefined}
          >
            {t(item.key)}
          </Link>
        ))}
      </nav>
      <span className="shell-spacer" />
      <StatusPill />
    </header>
  );
}
