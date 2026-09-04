import { useCallback, useEffect, useState } from "react";

import {
  api,
  type ResolvedMatch,
  type ResolvedMatchesResponse,
} from "@/api/client";
import { Badge, matchTypeTone, statusTone } from "@/components/ui/badge";
import { ConfidenceMeter } from "@/components/ui/confidence";
import { EmptyState } from "@/components/ui/empty-state";
import { CheckCircleIcon } from "@/components/ui/icons";
import { TableSkeleton } from "@/components/ui/skeleton";
import { cn, formatDateTime, formatINR } from "@/lib/utils";

type SortField = "resolved_at" | "proposed_at" | "confidence_score";

const SELECT_CLASS =
  "rounded-md border border-hairline bg-raised px-2.5 py-2 text-[13px] text-slate-300 transition-colors focus:border-green-500/70 focus:outline-none";

const MATCH_TYPES: { value: string; label: string }[] = [
  { value: "", label: "All stages" },
  { value: "deterministic", label: "Deterministic" },
  { value: "fuzzy", label: "Fuzzy" },
  { value: "semantic", label: "Semantic" },
  { value: "ai", label: "AI" },
];

const STAGE_TONE: Record<string, "neutral" | "ai" | "info" | "success"> = {
  deterministic: "success",
  fuzzy: "info",
  semantic: "ai",
  ai: "ai",
};

function SortHeader({
  label,
  field,
  sort,
  onSort,
  align,
}: {
  label: string;
  field: SortField;
  sort: { by: SortField; order: "asc" | "desc" };
  onSort: (field: SortField) => void;
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

export function ResolvedPage({
  refreshSignal,
  onOpenMatch,
}: {
  refreshSignal: number;
  onOpenMatch?: (matchId: string) => void;
}) {
  const [data, setData] = useState<ResolvedMatchesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [matchType, setMatchType] = useState("");
  const [sort, setSort] = useState<{ by: SortField; order: "asc" | "desc" }>({
    by: "resolved_at",
    order: "desc",
  });

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.resolvedMatches({
        resolution: "auto",
        match_type: matchType || undefined,
        sort_by: sort.by,
        order: sort.order,
        limit: 250,
      });
      setData(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load resolved matches");
    } finally {
      setLoading(false);
    }
  }, [matchType, sort]);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  function toggleSort(field: SortField) {
    setSort((prev) =>
      prev.by === field
        ? { by: field, order: prev.order === "desc" ? "asc" : "desc" }
        : { by: field, order: "desc" },
    );
  }

  const items: ResolvedMatch[] = data?.items ?? [];
  const shownItems = items.slice(0, 20);
  const truncated = items.length > shownItems.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-4">
        <div className="mr-auto flex items-center gap-2.5">
          <span className="microlabel">Auto-resolved</span>
          <span className="num rounded border border-hairline bg-raised px-1.5 py-px text-xs text-success">
            {data?.auto ?? "…"} total
          </span>
        </div>
        <select
          value={matchType}
          onChange={(e) => setMatchType(e.target.value)}
          className={SELECT_CLASS}
        >
          {MATCH_TYPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
        <p className="border-b border-hairline px-5 py-3 text-xs text-slate-500">
          Matches confirmed by the pipeline without human input. The stage column shows which
          matcher resolved each one — deterministic, fuzzy, semantic, or AI. Click a row to see the
          exact evidence that settled it.
        </p>

        {error && (
          <EmptyState icon={<CheckCircleIcon className="h-5 w-5" />} title="Couldn't load auto-resolved matches" hint={error} />
        )}

        {!error && loading && <TableSkeleton rows={8} columns={5} />}

        {!error && !loading && items.length === 0 && (
          <EmptyState
            icon={<CheckCircleIcon className="h-5 w-5" />}
            title="No auto-resolved matches yet"
            hint="Run a reconciliation to auto-resolve matches. They will appear here grouped by the stage that settled them."
          />
        )}

        {!error && !loading && items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-hairline">
                  <th className="microlabel px-5 py-3.5 font-medium">Match</th>
                  <SortHeader label="Stage" field="confidence_score" sort={sort} onSort={() => toggleSort("confidence_score")} />
                  <SortHeader label="Resolved" field="resolved_at" sort={sort} onSort={() => toggleSort("resolved_at")} />
                  <SortHeader label="Proposed" field="proposed_at" sort={sort} onSort={() => toggleSort("proposed_at")} />
                  <th className="microlabel px-3 py-3.5 text-right font-medium">Amount</th>
                  <th className="microlabel hidden px-5 py-3.5 font-medium lg:table-cell">Confidence</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/40">
                {shownItems.map((match) => {
                  const total = match.participants.reduce((a, p) => a + Number(p.amount), 0);
                  const tone = STAGE_TONE[match.stage ?? ""] ?? "neutral";
                  return (
                    <tr
                      key={match.match_id}
                      onClick={() => onOpenMatch?.(match.match_id)}
                      title="Click to see exactly how this was resolved"
                      className="group cursor-pointer transition-colors hover:bg-gloss"
                    >
                      <td className="max-w-72 px-5 py-3.5">
                        <div className="flex items-center gap-2">
                          <Badge tone={matchTypeTone(match.match_type)}>{match.match_type}</Badge>
                          <Badge tone={statusTone(match.status)}>{match.status}</Badge>
                        </div>
                        <p className="num mt-1 flex flex-wrap gap-x-2 text-xs text-slate-500" title={match.match_id}>
                          {match.participants.map((p) => (
                            <span key={p.transaction_id} className="max-w-40 truncate">
                              <span className="text-slate-600">{p.source}:</span>
                              {p.external_ref}
                            </span>
                          ))}
                        </p>
                      </td>
                      <td className="whitespace-nowrap px-3 py-3.5">
                        {match.stage ? (
                          <Badge tone={tone}>{match.stage}</Badge>
                        ) : (
                          <span className="text-xs text-slate-600">—</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-3 py-3.5 text-xs text-slate-500">
                        {match.resolved_at ? formatDateTime(match.resolved_at) : "—"}
                      </td>
                      <td className="whitespace-nowrap px-3 py-3.5 text-xs text-slate-500">
                        {formatDateTime(match.proposed_at)}
                      </td>
                      <td className="whitespace-nowrap px-3 py-3.5 text-right">
                        <span className="num text-sm font-medium text-slate-200">
                          {formatINR(total)}
                        </span>
                        <span className="num block text-xs text-slate-600">{match.participants.length} legs</span>
                      </td>
                      <td className="hidden whitespace-nowrap px-5 py-3.5 lg:table-cell">
                        <ConfidenceMeter value={Number(match.confidence_score)} />
                        {match.rationale && (
                          <p className="mt-1 max-w-56 truncate text-xs text-slate-600" title={match.rationale}>
                            {match.rationale}
                          </p>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {truncated && !loading && (
          <p className="border-t border-hairline px-5 py-3 text-xs text-slate-600">
            Showing {shownItems.length} of {items.length} — refine the filters to narrow the list.
          </p>
        )}
      </section>
    </div>
  );
}
