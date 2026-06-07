"use client";
import { Sun, Moon } from "lucide-react";
import { useEffect, useState } from "react";
import { getTheme, toggleTheme, type Theme } from "@/lib/theme";

export function ThemeToggle() {
  const [theme, setLocal] = useState<Theme>("light");
  useEffect(() => {
    setLocal(getTheme());
    const onChange = (e: Event) => {
      const detail = (e as CustomEvent<Theme>).detail;
      if (detail) setLocal(detail);
    };
    window.addEventListener("taiex:theme", onChange);
    return () => window.removeEventListener("taiex:theme", onChange);
  }, []);
  return (
    <button
      type="button"
      className="icon-btn theme-toggle"
      aria-label="切換主題"
      title={`切換主題 (${theme === "dark" ? "Dark" : "Light"})`}
      onClick={() => toggleTheme()}
    >
      <Sun
        size={16}
        className="theme-toggle-icon theme-toggle-icon--sun"
        aria-hidden
      />
      <Moon
        size={16}
        className="theme-toggle-icon theme-toggle-icon--moon"
        aria-hidden
      />
    </button>
  );
}
