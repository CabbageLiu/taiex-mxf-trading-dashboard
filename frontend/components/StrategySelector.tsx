"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Settings } from "lucide-react";

import { api, type StrategyOut } from "@/lib/api";
import { t } from "@/lib/i18n";
import { StrategyParamsPopover } from "./StrategyParamsPopover";

/**
 * Searchable combobox over the registered strategies.
 *
 * The "active" strategy is a UI concept: it's persisted in the URL via the
 * `?s=<name>` query param so deep-linking and cross-page navigation keep
 * the same strategy in focus on /trading and /analysis.
 *
 * Each row exposes:
 *   - name + small enable toggle (POST /strategies/{name}/enable)
 *   - gear icon → opens StrategyParamsPopover (PATCH /strategies/{name}/params)
 *
 * Keyboard:
 *   - Arrow up/down nav list
 *   - Enter selects (sets active + closes)
 *   - Esc closes
 */
export function StrategySelector() {
  const router = useRouter();
  const pathname = usePathname();
  const search = useSearchParams();
  const activeName = search.get("s");

  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [highlight, setHighlight] = useState(0);
  const [editing, setEditing] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const qc = useQueryClient();
  const { data, isLoading } = useQuery({
    queryKey: ["strategies"],
    queryFn: api.strategies,
    refetchInterval: 10_000,
  });

  const items = useMemo(() => {
    const list = data ?? [];
    if (!query) return list;
    const q = query.toLowerCase();
    return list.filter((s) => s.name.toLowerCase().includes(q));
  }, [data, query]);

  // Clamp highlight when items change
  useEffect(() => {
    if (highlight >= items.length) setHighlight(Math.max(0, items.length - 1));
  }, [items.length, highlight]);

  // Click-outside closes the listbox (but not while a popover is open)
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(e.target as Node)) {
        setOpen(false);
        setEditing(null);
      }
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const setActive = (name: string) => {
    const next = new URLSearchParams(Array.from(search.entries()));
    next.set("s", name);
    router.replace(`${pathname}?${next.toString()}`);
  };

  const toggleEnable = useMutation({
    mutationFn: ({ name, enabled }: { name: string; enabled: boolean }) => api.enableStrategy(name, enabled),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["strategies"] }),
  });

  const onKeyDown: React.KeyboardEventHandler<HTMLInputElement> = (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setHighlight((h) => Math.min(h + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => Math.max(h - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const pick = items[highlight];
      if (pick) {
        setActive(pick.name);
        setOpen(false);
        setQuery("");
        inputRef.current?.blur();
      }
    } else if (e.key === "Escape") {
      setOpen(false);
      setEditing(null);
    }
  };

  // Display value: active strategy name, or current query while typing
  const displayValue = open ? query : (activeName ?? "");

  const editingStrategy = editing ? (data ?? []).find((s) => s.name === editing) ?? null : null;

  return (
    <div className="combo" ref={containerRef}>
      <input
        ref={inputRef}
        type="text"
        className="combo-input"
        placeholder={t("panel_strategies")}
        value={displayValue}
        onFocus={() => setOpen(true)}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onKeyDown={onKeyDown}
        aria-autocomplete="list"
        aria-expanded={open}
      />
      {open && (
        <ul className="combo-listbox" role="listbox">
          {isLoading && (
            <li className="combo-row" style={{ color: "var(--ink-muted)" }}>{t("state_loading")}</li>
          )}
          {!isLoading && items.length === 0 && (
            <li className="combo-row" style={{ color: "var(--ink-muted)" }}>{t("state_none_strategies")}</li>
          )}
          {items.map((s: StrategyOut, idx: number) => (
            <li
              key={s.name}
              className={`combo-row${activeName === s.name ? " is-active" : ""}`}
              role="option"
              aria-selected={highlight === idx}
              onMouseEnter={() => setHighlight(idx)}
              onClick={() => { setActive(s.name); setOpen(false); setQuery(""); }}
            >
              <div>
                <div>{s.name}</div>
                <div className="meta">{s.resolutions.join(", ")}</div>
              </div>
              <div className="controls" onClick={(e) => e.stopPropagation()}>
                <button
                  className="btn"
                  aria-pressed={s.enabled}
                  onClick={() => toggleEnable.mutate({ name: s.name, enabled: !s.enabled })}
                  style={{ minHeight: 28, padding: "2px 8px", fontSize: "var(--fs-caption)" }}
                >
                  {s.enabled ? t("btn_on") : t("btn_off")}
                </button>
                <button
                  className="icon-btn"
                  aria-label="settings"
                  onClick={() => setEditing(s.name)}
                  title="settings"
                >
                  <Settings size={16} aria-hidden />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      {editingStrategy && (
        <StrategyParamsPopover strategy={editingStrategy} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}
