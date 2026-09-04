import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { CommandIcon, CornerDownLeftIcon, SearchIcon } from "@/components/ui/icons";
import { cn } from "@/lib/utils";

export interface PaletteAction {
  id: string;
  label: string;
  hint?: string;
  keywords?: string[];
  icon?: ReactNode;
  run: () => void;
}

interface GroupedAction {
  group: string;
  items: PaletteAction[];
}

/**
 * Cmd/Ctrl+K command palette — a fast, keyboard-first way to jump anywhere
 * or trigger a system action. Presentational: the caller builds the action
 * list, so it can drive tabs, runs, and exports without owning navigation.
 */
export function CommandPalette({
  open,
  actions,
  onClose,
}: {
  open: boolean;
  actions: GroupedAction[];
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const flat = useMemo(
    () => actions.flatMap((group) => group.items.map((item) => ({ ...item, group: group.group }))),
    [actions],
  );

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return flat;
    return flat.filter(
      (item) =>
        item.label.toLowerCase().includes(q) ||
        item.hint?.toLowerCase().includes(q) ||
        (item.keywords ?? []).some((k) => k.toLowerCase().includes(q)),
    );
  }, [flat, query]);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setIndex(0);
    const raf = requestAnimationFrame(() => inputRef.current?.focus());
    return () => cancelAnimationFrame(raf);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Keep selection within the current match set and visible.
  useEffect(() => {
    if (index >= matches.length) setIndex(0);
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${index}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [index, matches.length]);

  if (!open) return null;

  function run(item: PaletteAction) {
    onClose();
    item.run();
  }

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center px-4 pt-[12vh]" role="dialog" aria-modal="true" aria-label="Command palette">
      <button aria-label="close command palette" onClick={onClose} className="absolute inset-0 bg-overlay backdrop-blur-[2px]" />
      <div className="animate-slides-down relative w-full max-w-lg overflow-hidden rounded-xl border border-hairline bg-surface shadow-[0_24px_64px_rgba(0,0,0,0.5)]">
        <div className="flex items-center gap-2.5 border-b border-hairline px-3.5">
          <SearchIcon className="h-4 w-4 shrink-0 text-slate-500" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setIndex(0);
            }}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setIndex((i) => Math.min(i + 1, matches.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setIndex((i) => Math.max(i - 1, 0));
              } else if (e.key === "Enter" && matches[index]) {
                run(matches[index]);
              }
            }}
            placeholder="Jump to a view or action…"
            className="h-13 w-full bg-transparent text-base text-slate-100 placeholder:text-slate-600 focus:outline-none"
          />
        </div>

        {matches.length === 0 ? (
          <p className="px-4 py-9 text-center text-sm text-slate-600">No matches for “{query}”</p>
        ) : (
          <ul ref={listRef} className="max-h-[46vh] overflow-y-auto py-1.5" role="listbox" aria-label="Actions">
            {matches.map((item, i) => (
              <li key={item.id} role="option" aria-selected={i === index}>
                <button
                  data-index={i}
                  onClick={() => run(item)}
                  onMouseEnter={() => setIndex(i)}
                  className={cn(
                    "flex w-full items-center gap-3 px-3.5 py-2.5 text-left transition-colors",
                    i === index ? "bg-gloss-strong" : "hover:bg-gloss",
                  )}
                >
                  {item.icon && (
                    <span className={cn("flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border", i === index ? "border-success/40 bg-success/15 text-success" : "border-hairline bg-raised text-slate-400")}>
                      {item.icon}
                    </span>
                  )}
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-slate-100">{item.label}</span>
                    {item.hint && <span className="block truncate text-xs text-slate-500">{item.hint}</span>}
                  </span>
                  {i === index && <CornerDownLeftIcon className="h-4 w-4 shrink-0 text-success/70" />}
                </button>
              </li>
            ))}
          </ul>
        )}

        <footer className="flex items-center gap-3 border-t border-hairline bg-canvas/40 px-3.5 py-2.5 text-xs text-slate-600">
          <span className="flex items-center gap-1">
            <kbd className="rounded border border-hairline bg-raised px-1 py-px font-mono">↑</kbd>
            <kbd className="rounded border border-hairline bg-raised px-1 py-px font-mono">↓</kbd>
            navigate
          </span>
          <span className="flex items-center gap-1">
            <kbd className="rounded border border-hairline bg-raised px-1 py-px font-mono">↵</kbd>
            select
          </span>
          <span className="ml-auto flex items-center gap-1">
            <kbd className="rounded border border-hairline bg-raised px-1 py-px font-mono">esc</kbd>
            close
          </span>
        </footer>
      </div>
    </div>
  );
}

export function PaletteTriggerIcon() {
  return <CommandIcon className="h-4 w-4" />;
}

export function PaletteTrigger({ onClick, compact = false }: { onClick: () => void; compact?: boolean }) {
  return (
    <button
      onClick={onClick}
      title="Command palette (Ctrl/⌘K)"
      className={cn(
        "flex items-center gap-1.5 rounded-md border border-hairline bg-surface text-slate-500 transition-colors hover:border-slate-600 hover:text-slate-300",
        compact ? "h-8 px-2" : "h-8 px-3",
      )}
    >
      <PaletteTriggerIcon />
      {!compact && (
        <span className="flex items-center gap-1 text-xs">
          <kbd className="font-mono">⌘</kbd>
          <kbd className="font-mono">K</kbd>
        </span>
      )}
    </button>
  );
}