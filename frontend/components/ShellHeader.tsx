"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { dict, t } from "@/lib/i18n";
import { StatusPill } from "./StatusPill";

const NAV: { href: string; key: "nav.trading" | "nav.analysis" }[] = [
  { href: "/trading", key: "nav.trading" },
  { href: "/analysis", key: "nav.analysis" },
];

function ShellNav() {
  const pathname = usePathname() ?? "";
  const search = useSearchParams();
  const qs = search.toString();
  const isActive = (href: string) => pathname === href || pathname.startsWith(`${href}/`);
  return (
    <nav className="shell-nav" aria-label="primary">
      {NAV.map((item) => (
        <Link
          key={item.href}
          href={qs ? `${item.href}?${qs}` : item.href}
          className={`nav-link${isActive(item.href) ? " active" : ""}`}
          aria-current={isActive(item.href) ? "page" : undefined}
        >
          {t(item.key)}
        </Link>
      ))}
    </nav>
  );
}

export function ShellHeader() {
  return (
    <header className="shell-header">
      <strong className="brand">{dict.app_title}</strong>
      <Suspense fallback={<nav className="shell-nav" aria-label="primary" />}>
        <ShellNav />
      </Suspense>
      <span className="shell-spacer" />
      <StatusPill />
    </header>
  );
}
