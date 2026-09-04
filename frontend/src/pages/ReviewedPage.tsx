import { useCallback, useEffect, useState } from "react";

import {
  api,
  type ReviewedItem,
  type ReviewedItemsResponse,
} from "@/api/client";
import { Badge, matchTypeTone } from "@/components/ui/badge";
import { ConfidenceMeter } from "@/components/ui/confidence";
import { EmptyState } from "@/components/ui/empty-state";
import { UserIcon } from "@/components/ui/icons";
import { TableSkeleton } from "@/components/ui/skeleton";
import { cn, formatDateTime, formatINR } from "@/lib/utils";

const SELECT_CLASS =
  "rounded-md border border-hairline bg-raised px-2.5 py-2 text-[13px] text-slate-300 transition-colors focus:border-green-500/70 focus:outline-none";

const ACTIONS: { value: string; label: string }[] = [
  { value: "", label: "All actions" },
  { value: "approved", label: "Approved" },
  { value: "manually matched", label: "Manually matched" },
  { value: "rejected", label: "Rejected" },
  { value: "dismissed", label: "Dismissed" },
];

const ITEM_TYPES: { value: string; label: string }[] = [
  { value: "", label: "All items" },
  { value: "match", label: "Matches" },
  { value: "exception", label: "Exceptions" },
];

const ACTION_TONE: Record<string, "success" | "info" | "danger" | "warning"> = {
  approved: "success",
  "manually matched": "info",
  rejected: "danger",
  dismissed: "warning",
};

const ACTION_SUB: Record<string, string> = {
  approved: "Confirmed the suggestion",
  "manually matched": "Built a different match",
  rejected: "Overrode the suggestion",
  dismissed: "Judged a false positive",
};

function actionTone(action: string): "success" | "info" | "danger" | "warning" {
  return ACTION_TONE[action] ?? "neutral";
}

export function ReviewedPage({ refreshSignal }: { refreshSignal: number }) {
  const [data, setData] = useState<ReviewedItemsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [itemType, setItemType] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.reviewedItems({
        action: action || undefined,
        actor: actor || undefined,
        item_type: (itemType || undefined) as "match" | "exception" | undefined,
        limit: 250,
      });
      setData(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load reviewed items");
    } finally {
      setLoading(false);
    }
  }, [action, actor, itemType]);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  const items: ReviewedItem[] = data?.items ?? [];
  const shownItems = items.slice(0, 20);
  const truncated = items.length > shownItems.length;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="mr-auto flex items-center gap-2.5">
          <span className="microlabel">Reviewed</span>
          <span className="num rounded border border-hairline bg-raised px-1.5 py-px text-xs text-human">
            {data?.match_count ?? "…"} matches
          </span>
          <span className="num rounded border border-hairline bg-raised px-1.5 py-px text-xs text-warning">
            {data?.exception_count ?? "…"} dismissed
          </span>
        </div>
        <select
          value={itemType}
          onChange={(e) => setItemType(e.target.value)}
          className={SELECT_CLASS}
        >
          {ITEM_TYPES.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select value={action} onChange={(e) => setAction(e.target.value)} className={SELECT_CLASS}>
          {ACTIONS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <input
          value={actor}
          onChange={(e) => setActor(e.target.value)}
          placeholder="actor"
          className="w-28 rounded-md border border-hairline bg-raised px-2.5 py-2 text-[13px] text-slate-300 placeholder:text-slate-600 focus:border-green-500/70 focus:outline-none"
        />
      </div>

      <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
        <p className="border-b border-hairline px-5 py-3 text-xs text-slate-500">
          Every decision a human has made, attributed from the audit trail. The action label shows
          whether the reviewer confirmed the pipeline's suggestion (approved), overrode it
          (rejected / manually matched), or dismissed an exception as a false positive — with who,
          when, and their note.
        </p>

        {error && (
          <EmptyState icon={<UserIcon className="h-5 w-5" />} title="Couldn't load reviewed items" hint={error} />
        )}

        {!error && loading && <TableSkeleton rows={8} columns={6} />}

        {!error && !loading && items.length === 0 && (
          <EmptyState
            icon={<UserIcon className="h-5 w-5" />}
            title="No human decisions yet"
            hint="Take an action in the To Be Reviewed tab — approve, reject, manually match, or dismiss — and the decision will appear here."
          />
        )}

        {!error && !loading && items.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-hairline">
                  <th className="microlabel px-5 py-3.5 font-medium">Item</th>
                  <th className="microlabel px-3 py-3.5 font-medium">Human decision</th>
                  <th className="microlabel px-3 py-3.5 font-medium">Who</th>
                  <th className="microlabel px-3 py-3.5 font-medium">Reviewed</th>
                  <th className="microlabel hidden px-3 py-3.5 font-medium lg:table-cell">Note</th>
                  <th className="microlabel hidden px-5 py-3.5 text-right font-medium lg:table-cell">Confidence</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/40">
                {shownItems.map((item) => {
                  const isMatch = item.item_type === "match";
                  const amount = isMatch
                    ? item.participants.reduce((a, p) => a + Number(p.amount), 0)
                    : Number(item.amount_impact ?? 0);
                  const refs = isMatch
                    ? item.participants.map((p) => p.external_ref)
                    : item.transaction_ref
                      ? [item.transaction_ref]
                      : [];
                  const title = isMatch ? (item.match_type ?? "match") : (item.exception_type ?? "exception").replace(/_/g, " ");
                  const tone = actionTone(item.action);
                  return (
                    <tr key={`${item.item_type}-${item.id}`} className="group transition-colors hover:bg-gloss">
                      <td className="max-w-72 px-5 py-3.5">
                        <div className="flex items-center gap-2">
                          <Badge tone={isMatch ? matchTypeTone(item.match_type) : "warning"}>
                            {isMatch ? "match" : "exception"}
                          </Badge>
                          <span className="capitalize text-slate-300">{title}</span>
                        </div>
                        <p className="num mt-1 flex flex-wrap gap-x-2 text-xs text-slate-500">
                          {refs.map((ref, i) => (
                            <span key={i} className="max-w-40 truncate">{ref}</span>
                          ))}
                          {refs.length === 0 && <span className="text-slate-700">—</span>}
                        </p>
                      </td>
                      <td className="whitespace-nowrap px-3 py-3.5">
                        <div className="space-y-1">
                          <span className="flex items-center gap-1.5">
                            <Badge tone={tone}>{item.action}</Badge>
                          </span>
                          <span
                            className={cn(
                              "text-xs",
                              item.action === "rejected" || item.action === "dismissed"
                                ? "text-critical/70"
                                : "text-success/70",
                            )}
                          >
                            {ACTION_SUB[item.action] ?? item.action}
                          </span>
                        </div>
                      </td>
                      <td className="whitespace-nowrap px-3 py-3.5 text-[13px] text-slate-400">
                        {item.actor ?? "—"}
                      </td>
                      <td className="whitespace-nowrap px-3 py-3.5 text-[13px] text-slate-500">
                        {item.reviewed_at ? formatDateTime(item.reviewed_at) : "—"}
                      </td>
                      <td className="hidden max-w-72 truncate px-3 py-3.5 lg:table-cell">
                        {item.note ? (
                          <span className="text-xs text-slate-500" title={item.note}>
                            {item.note}
                          </span>
                        ) : (
                          <span className="text-slate-700">—</span>
                        )}
                      </td>
                      <td className="hidden whitespace-nowrap px-5 py-3.5 text-right lg:table-cell">
                        {isMatch && item.confidence_score ? (
                          <div className="flex items-center justify-end gap-3">
                            <ConfidenceMeter value={Number(item.confidence_score)} />
                            <span className="num text-sm font-medium text-slate-200">
                              {formatINR(amount)}
                            </span>
                          </div>
                        ) : (
                          <span className="num text-sm font-medium text-slate-200">
                            {formatINR(amount)}
                          </span>
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
