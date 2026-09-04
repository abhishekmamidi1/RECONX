import { useCallback, useEffect, useState } from "react";

import {
  api,
  downloadReport,
  type IngestionRecord,
  type ReportSummary,
  type WebhookDelivery,
} from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AlertIcon, ArrowRightIcon, CheckCircleIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatDateTime } from "@/lib/utils";

type PresetKey = "this_week" | "last_7d" | "this_month" | "all" | "custom";

function rangeFor(preset: PresetKey): { from?: string; to?: string; label: string } {
  const today = new Date();
  const iso = (d: Date) => d.toISOString().slice(0, 10);
  switch (preset) {
    case "this_week": {
      const day = today.getUTCDay();
      const monday = new Date(today);
      monday.setUTCDate(today.getUTCDate() - ((day + 6) % 7));
      return { from: iso(monday), to: iso(today), label: "This week" };
    }
    case "last_7d": {
      const week_ago = new Date(today);
      week_ago.setUTCDate(today.getUTCDate() - 6);
      return { from: iso(week_ago), to: iso(today), label: "Last 7 days" };
    }
    case "this_month":
      return {
        from: iso(new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))),
        to: iso(today),
        label: "This month",
      };
    case "custom":
      return { label: "Custom" };
    default:
      return { label: "All time" };
  }
}

const PRESETS: PresetKey[] = ["this_week", "last_7d", "this_month", "all"];

export function ReportsPage({ refreshSignal }: { refreshSignal: number }) {
  const [preset, setPreset] = useState<PresetKey>("this_month");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");
  const [ingestionId, setIngestionId] = useState("");
  const [ingestions, setIngestions] = useState<IngestionRecord[]>([]);
  const [summary, setSummary] = useState<ReportSummary | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [flash, setFlash] = useState<{ ok: boolean; text: string } | null>(null);

  const [deliveries, setDeliveries] = useState<WebhookDelivery[] | null>(null);
  const [pushing, setPushing] = useState(false);

  const params = {
    ...(preset === "all"
      ? {}
      : preset === "custom"
        ? { from: customFrom || undefined, to: customTo || undefined }
        : (() => {
            const r = rangeFor(preset);
            return { from: r.from, to: r.to };
          })()),
    ingestion_id: ingestionId || undefined,
  };

  const loadAll = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([
        api.reportSummary(params),
        api.erpDeliveries(),
      ]);
      setSummary(s);
      setDeliveries(d);
      setFlash(null);
    } catch (err) {
      setFlash({ ok: false, text: err instanceof Error ? err.message : "failed to load reports" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(params), refreshSignal]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  useEffect(() => {
    api
      .ingestions()
      .then(setIngestions)
      .catch(() => setIngestions([]));
  }, []);

  async function generate(format: "csv" | "pdf") {
    setBusy(format);
    setFlash(null);
    try {
      const result = await downloadReport(format, params);
      setFlash({ ok: true, text: `${result.filename} downloaded (${(result.size / 1024).toFixed(1)} kB)` });
    } catch (err) {
      setFlash({ ok: false, text: err instanceof Error ? err.message : "report failed" });
    } finally {
      setBusy(null);
    }
  }

  async function pushToErp() {
    setPushing(true);
    setFlash(null);
    try {
      const result = await api.erpPush();
      setFlash({
        ok: true,
        text: `ERP webhook accepted ${result.pushed_items} items after ${result.attempts} attempt(s)`,
      });
      setDeliveries(await api.erpDeliveries());
    } catch (err) {
      setFlash({ ok: false, text: err instanceof Error ? err.message : "push failed" });
      try {
        setDeliveries(await api.erpDeliveries());
      } catch {
        // ledger refresh is best-effort
      }
    } finally {
      setPushing(false);
    }
  }

  return (
    <div className="space-y-5">
      {/* Range picker */}
      <section className="rounded-xl border border-hairline bg-surface p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="microlabel mr-2">Period</span>
          {PRESETS.map((key) => (
            <button
              key={key}
              onClick={() => setPreset(key)}
              className={cn(
                "rounded-md border px-3.5 py-2 text-sm transition-colors",
                preset === key
                  ? "border-success/50 bg-success/15 text-success"
                  : "border-hairline bg-raised text-slate-400 hover:text-slate-200",
              )}
            >
              {rangeFor(key).label}
            </button>
          ))}
          <button
            onClick={() => setPreset("custom")}
            className={cn(
              "rounded-md border px-3.5 py-2 text-sm transition-colors",
              preset === "custom"
                ? "border-success/50 bg-success/15 text-success"
                : "border-hairline bg-raised text-slate-400 hover:text-slate-200",
            )}
          >
            Custom
          </button>
          {preset === "custom" && (
            <span className="ml-1 flex items-center gap-1.5">
              <input
                type="date"
                value={customFrom}
                onChange={(e) => setCustomFrom(e.target.value)}
                className="num rounded-md border border-hairline bg-raised px-2 py-1.5 text-xs text-slate-200 focus:border-green-500/70 focus:outline-none"
              />
              <ArrowRightIcon className="h-3 w-3 text-slate-600" />
              <input
                type="date"
                value={customTo}
                onChange={(e) => setCustomTo(e.target.value)}
                className="num rounded-md border border-hairline bg-raised px-2 py-1.5 text-xs text-slate-200 focus:border-green-500/70 focus:outline-none"
              />
            </span>
          )}

          <span className="ml-1 flex items-center gap-1.5">
            <span className="microlabel">Batch</span>
            <select
              value={ingestionId}
              onChange={(e) => setIngestionId(e.target.value)}
              className="max-w-56 rounded-md border border-hairline bg-raised px-2 py-1.5 text-xs text-slate-300 focus:border-green-500/70 focus:outline-none"
            >
              <option value="">All batches</option>
              {ingestions.map((ingestion) => (
                <option key={ingestion.id} value={ingestion.id}>
                  {ingestion.filename} · {formatDateTime(ingestion.created_at)}
                </option>
              ))}
            </select>
          </span>

          <span className="ml-auto flex gap-2">
            <Button onClick={() => generate("csv")} disabled={busy !== null} variant="secondary" size="sm">
              {busy === "csv" ? "Generating…" : "Export CSV"}
            </Button>
            <Button onClick={() => generate("pdf")} disabled={busy !== null} size="sm">
              {busy === "pdf" ? "Generating…" : "Export PDF report"}
            </Button>
          </span>
        </div>

        {flash && (
          <p
            className={cn(
              "mt-3 flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs",
              flash.ok
                ? "border-success/40 bg-success/10 text-success"
                : "border-critical/40 bg-critical/10 text-critical",
            )}
          >
            {flash.ok ? <CheckCircleIcon className="h-3 w-3" /> : <AlertIcon className="h-3 w-3" />}
            {flash.text}
          </p>
        )}

        {summary?.scope && (
          <p className="mt-3 flex items-center gap-1.5 rounded-md border border-human/40 bg-human/10 px-3 py-1.5 text-xs text-human">
            <CheckCircleIcon className="h-3 w-3" />
            Batch scope: {summary.scope.filename} ({summary.scope.transactions_in_batch}/
            {summary.scope.rows_total} transactions) — headline stats cover this batch only;
            {summary.cross_batch_participants && summary.cross_batch_participants.length > 0
              ? ` ${summary.cross_batch_participants.length} participant(s) from other batches included as match context.`
              : " no cross-batch participants."}
          </p>
        )}
      </section>

      {/* Summary preview */}
      {!summary ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <Skeleton key={i} className="h-24 rounded-xl" />
          ))}
        </div>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[
            {
              label: "Confirmed matches",
              value: summary.matches.filter((m) => m.status === "confirmed").reduce((a, m) => a + m.count, 0),
              sub: `auto ${summary.auto_resolved_total} · human ${summary.human_resolved_total}`,
            },
            {
              label: "Awaiting review",
              value: summary.matches.filter((m) => m.status === "proposed").reduce((a, m) => a + m.count, 0),
              sub: "proposals pending decision",
            },
            {
              label: "Open exceptions",
              value: summary.exceptions
                .filter((e) => ["open", "in_review", "escalated"].includes(e.status))
                .reduce((a, e) => a + e.count, 0),
              sub: `over 30d: ${summary.open_exception_aging.over_30d}`,
            },
            {
              label: "Razorpay match rate",
              value: `${((summary.match_rate_by_source.find((r) => r.source === "razorpay")?.rate ?? 0) * 100).toFixed(1)}%`,
              sub: summary.scope ? "in-batch confirmed coverage" : "in-period confirmed coverage",
            },
          ].map((card) => (
            <div key={card.label} className="rounded-xl border border-hairline bg-surface p-5">
              <p className="microlabel">{card.label}</p>
              <p className="num mt-2 text-[28px] font-semibold leading-none tracking-tight text-foreground">
                {card.value}
              </p>
              <p className="mt-2 text-xs text-slate-500">{card.sub}</p>
            </div>
          ))}
        </div>
      )}

      {/* ERP webhook */}
      <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-5 py-3.5">
          <div>
            <h2 className="microlabel">ERP webhook</h2>
            <p className="mt-1 text-xs text-slate-500">
              Pushes resolved results (confirmed &amp; rejected matches, closed exceptions) to the
              configured ERP endpoint. Target comes from{" "}
              <span className="num text-slate-400">ERP_WEBHOOK_URL</span>. Failures retry with
              backoff and are always audited.
            </p>
          </div>
          <Button onClick={pushToErp} disabled={pushing} size="sm">
            {pushing ? "Pushing…" : "Push resolved to ERP"}
          </Button>
        </header>

        {!deliveries ? (
          <div className="space-y-2 px-5 py-4">
            {[0, 1].map((i) => (
              <Skeleton key={i} className="h-8 w-full" />
            ))}
          </div>
        ) : deliveries.length === 0 ? (
          <p className="px-5 py-9 text-center text-sm text-slate-500">
            No webhook deliveries yet.
          </p>
        ) : (
          <ul className="divide-y divide-slate-800/40">
            {deliveries.map((delivery) => (
              <li key={delivery.id} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3 text-sm">
                <Badge tone={delivery.action === "webhook.delivered" ? "success" : "danger"}>
                  {delivery.action === "webhook.delivered" ? "delivered" : "failed"}
                </Badge>
                <span className="max-w-64 truncate font-mono text-xs text-slate-400">
                  {delivery.details?.url ?? "—"}
                </span>
                <span className="num text-slate-500">
                  attempt {delivery.details?.attempts ?? "?"}
                  {delivery.details?.status_code ? ` · HTTP ${delivery.details.status_code}` : ""}
                  {typeof delivery.details?.batch_size === "number"
                    ? ` · ${delivery.details.batch_size} items`
                    : ""}
                </span>
                <span className="ml-auto text-slate-500">{formatDateTime(delivery.created_at)}</span>
                {delivery.action === "webhook.failed" && delivery.details?.errors?.length ? (
                  <span className="w-full truncate text-xs text-critical/80">
                    {delivery.details.errors[delivery.details.errors.length - 1]}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
