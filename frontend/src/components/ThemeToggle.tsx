import { useEffect, useState } from "react";

import { MoonIcon, SunIcon } from "@/components/ui/icons";
import { cn } from "@/lib/utils";

const THEME_KEY = "recon.theme";

function readTheme(): "dark" | "light" {
  const attribute = document.documentElement.getAttribute("data-theme");
  if (attribute === "light" || attribute === "dark") return attribute;
  return "dark";
}

/**
 * Light/dark switch. The <html data-theme> attribute drives every token in
 * index.css; the choice is persisted so it survives reloads, and the inline
 * pre-paint script in index.html applies it before React mounts.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">(readTheme);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      // private mode — just don't persist
    }
  }, [theme]);

  const next = theme === "dark" ? "light" : "dark";

  return (
    <button
      onClick={() => setTheme(next)}
      title={`Switch to ${next} mode (currently ${theme})`}
      aria-label={`Switch to ${next} mode`}
      className={cn(
        "flex h-8 w-8 items-center justify-center rounded-md border transition-colors",
        theme === "light"
          ? "border-amber-400/60 bg-amber-400/10 text-amber-600 hover:bg-amber-400/20"
          : "border-hairline bg-surface text-amber-300 hover:border-amber-500/50 hover:text-amber-200",
      )}
    >
      {theme === "dark" ? <SunIcon className="h-4 w-4" /> : <MoonIcon className="h-4 w-4" />}
    </button>
  );
}