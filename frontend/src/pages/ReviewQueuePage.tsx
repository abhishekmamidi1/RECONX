import { useCallback, useEffect, useState } from "react";

import {
  api,
  getActor,
  type QueueItem,
  type QueueItemType,
  type ReviewQueueParams,
} from "@/api/client";
import { Badge, matchTypeTone, statusTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfidenceMeter } from "@/components/ui/confidence";
import { EmptyState } from "@/components/ui/empty-state";
import { InboxIcon, RefreshIcon } from "@/components/ui/icons";
import { TableSkeleton } from "@/components/ui/skeleton";
import { cn, formatDate, formatINR, isTypingTarget } from "@/lib/utils";

const SELECT_CLASS =
  "rounded-md border border-hairline bg-raised px-2.5 py-2 text-[13px] text-slate-300 transition-colors focus:border-green-500/70 focus:outline-none";

function SortHeader({
  label,
  field,
  sort,
  onSort,
  align,
}: {
  label: string;
  field: "amount_impact" | "opened_at";
  sort: { by: "amount_impact" | "opened_at"; order: "asc" | "desc" };
  onSort: (field: "amount_impact" | "opened_at") => void;
  align?: "right";
}) {
  const active = sort.by === field;
  return (
    <th
      className={cn(
        "cursor-pointer select-none px-3 py-3.5 text-[11px] font-medium uppercase tracking-[0.09em] transition-colors hover:text-slate-400",
        align === "right" && "text-right",
        active ? "text-success" : "text-slate-500",
      )}
      onClick={() => onSort(field)}
    >
      <span className="inline-flex items-center gap-1">
        {label}
        <span className={cn("text-[10px] leading-none", active ? "opacity-100" : "opacity-0")}>
          {sort.order === "desc" ? "▼" : "▲"}
        </span>
      </span>
    </th>
  );
}

const PRIORITY_BAR: Record<string, string> = {
  critical: "bg-rose-500",
  high: "bg-amber-500",
};

export function ReviewQueuePage({
  refreshSignal,
  onOpenItem,
}: {
  refreshSignal: number;
  onOpenItem: (kind: QueueItemType, id: string) => void;
}) {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [counts, setCounts] = useState({ exceptions: 0, proposals: 0 });
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState({
    item_type: "" as "" | QueueItemType,
    exception_type: "",
    priority: "",
    status: "",
  });
  const [sort, setSort] = useState<{
    by: "amount_impact" | "opened_at";
    order: "asc" | "desc";
  }>({ by: "opened_at", order: "desc" });
  const [activeIndex, setActiveIndex] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: ReviewQueueParams = {
        item_type: filters.item_type || undefined,
        exception_type: filters.exception_type || undefined,
        priority: filters.priority || undefined,
        sort_by: sort.by,
        order: sort.order,
      };
      if (filters.status) params.status = filters.status;
      const data = await api.reviewQueue(params);
      setItems(data.items);
      setCounts(data.counts);
      setActiveIndex(0);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load review queue");
    } finally {
      setLoading(false);
    }
  }, [filters, sort]);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  // Keyboard-first review: ↑/↓ walk the queue, ↵ opens the focused row.
  // Skipped while typing in the filters or elsewhere.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp" && event.key !== "Enter") return;
      if (isTypingTarget(event.target) || loading || items.length === 0) return;
      event.preventDefault();
      if (event.key === "ArrowDown") {
        setActiveIndex((i) => Math.min(i + 1, items.length - 1));
      } else if (event.key === "ArrowUp") {
        setActiveIndex((i) => Math.max(i - 1, 0));
      } else {
        const item = items[activeIndex];
        if (item) onOpenItem(item.item_type, item.id);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, loading, activeIndex, onOpenItem]);

  function toggleSort(field: "amount_impact" | "opened_at") {
    setSort((prev) =>
      prev.by === field
        ? { by: field, order: prev.order === "desc" ? "asc" : "desc" }
        : { by: field, order: "desc" },
    );
  }

  return (
    <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
      {/* Toolbar */}
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-hairline px-5 py-3.5">
        <h2 className="microlabel mr-auto flex items-center gap-2.5">
          Review queue
          <span className="flex items-center gap-1.5 font-normal normal-case tracking-normal">
            <span className="num rounded border border-hairline bg-raised px-1.5 py-px text-xs text-critical">
              {counts.exceptions} exceptions
            </span>
            <span className="num rounded border border-hairline bg-raised px-1.5 py-px text-xs text-human">
              {counts.proposals} proposals
            </span>
          </span>
        </h2>

        {[
          {
            value: filters.item_type,
            set: (v: string) => setFilters((f) => ({ ...f, item_type: v as "" | QueueItemType })),
            options: [
              ["", "All items"],
              ["exception", "Exceptions"],
              ["proposal", "Proposals"],
            ],
          },
          {
            value: filters.exception_type,
            set: (v: string) => setFilters((f) => ({ ...f, exception_type: v })),
            options: [
              ["", "All types"],
              ["unmatched", "Unmatched"],
              ["manual_review_required", "Manual review"],
              ["low_confidence_ai", "Low-conf AI"],
              ["amount_mismatch", "Amount mismatch"],
              ["duplicate_suspect", "Duplicate"],
            ],
          },
          {
            value: filters.priority,
            set: (v: string) => setFilters((f) => ({ ...f, priority: v })),
            options: [
              ["", "Any priority"],
              ["critical", "Critical"],
              ["high", "High"],
              ["medium", "Medium"],
              ["low", "Low"],
            ],
          },
          {
            value: filters.status,
            set: (v: string) => setFilters((f) => ({ ...f, status: v })),
            options: [
              ["", "Active"],
              ["open", "Open"],
              ["in_review", "In review"],
              ["escalated", "Escalated"],
              ["resolved", "Resolved"],
              ["dismissed", "Dismissed"],
            ],
          },
        ].map((select, index) => (
          <select
            key={index}
            value={select.value}
            onChange={(e) => select.set(e.target.value)}
            className={SELECT_CLASS}
          >
            {select.options.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        ))}

        <Button onClick={load} disabled={loading} variant="ghost" className="px-2.5 py-2">
          <RefreshIcon className={cn("h-4 w-4", loading && "animate-spin")} />
        </Button>

        <span className="hidden items-center gap-1.5 text-xs text-slate-600 xl:flex">
          <kbd className="rounded border border-hairline bg-raised px-1 py-px font-mono">↑</kbd>
          <kbd className="rounded border border-hairline bg-raised px-1 py-px font-mono">↓</kbd>
          <kbd className="rounded border border-hairline bg-raised px-1 py-px font-mono">↵</kbd>
          navigate &amp; open
        </span>
      </header>

      {error && (
        <p className="px-5 py-6 text-sm text-critical">{error}</p>
      )}

      {!error && loading && <TableSkeleton rows={7} columns={7} />}

      {!error && !loading && items.length === 0 && (
        <EmptyState
          icon={<InboxIcon className="h-5 w-5" />}
          title="Queue clear"
          hint="Nothing is waiting on a human decision. New exceptions and low-confidence proposals appear here automatically after each reconciliation run."
        />
      )}

      {!error && !loading && items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-hairline">
                <th className="w-1 px-0 py-0" aria-hidden />
                <th className="microlabel px-2 py-3.5 font-medium">Kind</th>
                <th className="microlabel px-3 py-3.5 font-medium">Item</th>
                <th className="microlabel hidden px-3 py-3.5 font-medium md:table-cell">Records</th>
                <th className="microlabel px-3 py-3.5 font-medium">Status</th>
                <SortHeader label="Impact" field="amount_impact" sort={sort} onSort={toggleSort} align="right" />
                <SortHeader label="Opened" field="opened_at" sort={sort} onSort={toggleSort} />
                <th className="microlabel hidden px-5 py-3.5 font-medium lg:table-cell">Confidence</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/40">
              {items.map((item, i) => {
                const urgent = PRIORITY_BAR[item.priority] !== undefined;
                const active = i === activeIndex;
                return (
                  <tr
                    key={`${item.item_type}-${item.id}`}
                    onClick={() => {
                      setActiveIndex(i);
                      onOpenItem(item.item_type, item.id);
                    }}
                    onMouseEnter={() => setActiveIndex(i)}
                    aria-current={active ? "true" : undefined}
                    className={cn(
                      "cursor-pointer transition-colors hover:bg-gloss",
                      urgent && "bg-gloss",
                      active && "bg-gloss-strong shadow-[inset_2px_0_0_var(--color-success)]",
                    )}
                  >
                    <td className="relative w-1 px-0 py-0">
                      <span
                        className={cn(
                          "absolute inset-y-0 left-0 w-[3px]",
                          PRIORITY_BAR[item.priority],
                        )}
                      />
                      {urgent && <span className="sr-only">{item.priority} priority</span>}
                    </td>
                    <td className="whitespace-nowrap px-2 py-3.5">
                      {item.item_type === "exception" ? (
                        <Badge tone="danger" className="border-slate-700/60 bg-transparent text-slate-400">
                          exc
                        </Badge>
                      ) : (
                        <Badge tone={matchTypeTone(item.match_type)}>
                          {item.match_type === "ai" ||
                          item.match_type === "semantic" ||
                          item.match_type === "batch"
                            ? "AI"
                            : "prop"}
                        </Badge>
                      )}
                    </td>
                    <td className="max-w-44 truncate px-3 py-3.5">
                      <span className={cn("capitalize", urgent ? "font-medium text-slate-100" : "text-slate-300")}>
                        {item.title}
                      </span>
                    </td>
                    <td className="hidden max-w-60 truncate px-3 py-3.5 md:table-cell">
                      <span className="num text-xs text-slate-500">
                        {item.refs.join("  +  ") || "—"}
                      </span>
                    </td>
                    <td className="px-3 py-3.5">
                      <Badge tone={statusTone(item.status)}>
                        {item.status.replace(/_/g, " ")}
                      </Badge>
                    </td>
                    <td className="whitespace-nowrap px-3 py-3.5 text-right">
                      <span className={cn("num text-sm", urgent ? "font-semibold text-slate-100" : "font-medium text-slate-300")}>
                        {formatINR(item.amount_impact ?? "0")}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-3.5 text-xs text-slate-500">
                      {formatDate(item.opened_at)}
                    </td>
                    <td className="hidden px-5 py-2 lg:table-cell">
                      {item.confidence ? (
                        <ConfidenceMeter value={item.confidence} />
                      ) : (
                        <span className="text-xs text-slate-700">—</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {!getActor() && items.length > 0 && (
        <footer className="flex items-center gap-2 border-t border-hairline bg-raised/30 px-5 py-3 text-xs text-warning">
          <span className="h-1.5 w-1.5 rounded-full bg-warning" />
          Set your actor id in the header before acting — every decision is written to the audit trail.
        </footer>
      )}
    </section>
  );
}
