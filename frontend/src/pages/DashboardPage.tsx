import { useCallback, useEffect, useId, useState } from "react";

import {
  api,
  downloadReport,
  type AnalyticsOverview,
  type AuditEntry,
  type DashboardSummary,
  type LoopCloseReport,
  type LoopCloseException,
} from "@/api/client";
import {
  ChartCard,
  ChartsSkeleton,
  ExceptionFlowChart,
  MatcherBars,
  ResolutionFlowChart,
  SplitDonut,
} from "@/components/analytics";
import {
  ActivityIcon,
  AlertIcon,
  CheckCircleIcon,
  FileTextIcon,
} from "@/components/ui/icons";
import { Button } from "@/components/ui/button";
import { StatCardSkeleton } from "@/components/ui/skeleton";
import { cn, timeAgo } from "@/lib/utils";
import { useCountUp, useNow } from "@/lib/hooks";

const PRIORITY_ORDER = ["critical", "high", "medium", "low"] as const;

function priorityChipClass(priority: string): string {
  switch (priority) {
    case "critical":
      return "border-critical/40 bg-critical/10 text-critical";
    case "high":
      return "border-warning/40 bg-warning/10 text-warning";
    case "medium":
      return "border-slate-700/70 bg-slate-800/40 text-slate-300";
    default:
      return "border-slate-800 bg-slate-900 text-slate-500";
  }
}

type Period = 7 | 30 | 90;
const PERIODS: Period[] = [7, 30, 90];

function isoDaysAgo(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - (days - 1));
  return d.toISOString().slice(0, 10);
}

/** Tiny inline trend line under each stat — the number plus its shape. */
function Sparkline({ points, color }: { points: number[]; color: string }) {
  const gid = useId();
  if (!points || points.length < 2) return null;
  const w = 96;
  const h = 26;
  const max = Math.max(...points, 1);
  const min = Math.min(...points, 0);
  const range = max - min || 1;
  const step = w / (points.length - 1);
  const coords = points.map((v, i) => [i * step, h - 2 - ((v - min) / range) * (h - 4)] as const);
  const line = coords
    .map(([x, y], i) => `${i === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`)
    .join(" ");
  const area = `${line} L${w},${h} L0,${h} Z`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="mt-3 h-7 w-full" aria-hidden>
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.35} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <path d={area} fill={`url(#${gid})`} />
      <path
        d={line}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}

function StatCard({
  label,
  value,
  accent,
  subtle,
  spark = null,
}: {
  label: string;
  value: number;
  accent: string;
  subtle?: React.ReactNode;
  spark?: { points: number[]; color: string } | null;
}) {
  const animated = useCountUp(value);
  return (
    <div className="card-interactive rounded-xl border border-hairline bg-surface p-6">
      <p className="microlabel">{label}</p>
      <p className={cn("num mt-3 text-[30px] font-semibold leading-none tracking-tight", accent)}>
        {animated.toLocaleString("en-IN")}
      </p>
      {spark && <Sparkline points={spark.points} color={spark.color} />}
      {subtle && <div className="mt-2.5 text-xs text-slate-500">{subtle}</div>}
    </div>
  );
}

function activityTone(action: string): "success" | "danger" | "info" {
  const a = action.toLowerCase();
  if (/(approve|confirm|deliver|push|match|resolve)/.test(a)) return "success";
  if (/(reject|dismiss|fail|escalat)/.test(a)) return "danger";
  return "info";
}

function prettyAction(action: string): string {
  const cleaned = action
    .replace(/^webhook\./, "webhook ")
    .replace(/\./g, " · ")
    .replace(/_/g, " ");
  return cleaned
    .split(" ")
    .map((word) => (word === "·" ? word : word.charAt(0).toUpperCase() + word.slice(1)))
    .join(" ");
}

/** Live audit trail — polls quietly so the dashboard feels reactive. */
function ActivityFeed() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [fetchedAt, setFetchedAt] = useState(() => Date.now());
  const now = useNow(15_000);

  useEffect(() => {
    let alive = true;
    async function tick() {
      try {
        const data = await api.auditLogs({ limit: 8 });
        if (alive) {
          setEntries(data);
          setFetchedAt(Date.now());
        }
      } catch {
        // the feed is best-effort; a dead feed shouldn't break the dashboard
      }
    }
    tick();
    const timer = setInterval(tick, 20_000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, []);

  const icons = {
    success: <CheckCircleIcon className="h-3.5 w-3.5" />,
    danger: <AlertIcon className="h-3.5 w-3.5" />,
    info: <ActivityIcon className="h-3.5 w-3.5" />,
  };

  return (
    <section className="card-interactive overflow-hidden rounded-xl border border-hairline bg-surface">
      <header className="flex items-center justify-between gap-3 border-b border-hairline px-5 py-4">
        <h2 className="microlabel flex items-center gap-2">
          Recent activity
          <span className="relative flex h-1.5 w-1.5 text-success">
            <span className="animate-live-ping relative block h-1.5 w-1.5 rounded-full bg-current" />
          </span>
        </h2>
        <span className="text-xs text-slate-600">
          {entries ? `updated ${timeAgo(new Date(fetchedAt).toISOString(), now)}` : "…"}
        </span>
      </header>

      {!entries ? (
        <div className="space-y-2 px-5 py-4">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="flex items-center gap-3">
              <div className="h-5 w-5 animate-pulse rounded-full bg-slate-800/60" />
              <div className="h-2.5 flex-1 animate-pulse rounded bg-slate-800/60" />
            </div>
          ))}
        </div>
      ) : entries.length === 0 ? (
        <p className="px-5 py-9 text-center text-sm text-slate-600">
          No audit activity recorded yet.
        </p>
      ) : (
        <ul className="divide-y divide-slate-800/40">
          {entries.map((entry) => {
            const tone = activityTone(entry.action);
            const tones = {
              success: "border-success/30 bg-success/10 text-success",
              danger: "border-critical/30 bg-critical/10 text-critical",
              info: "border-info/30 bg-info/10 text-info",
            } as const;
            return (
              <li key={entry.id} className="flex items-center gap-3 px-5 py-3">
                <span
                  className={cn(
                    "flex h-7 w-7 shrink-0 items-center justify-center rounded-md border",
                    tones[tone],
                  )}
                >
                  {icons[tone]}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] text-slate-200">
                    {prettyAction(entry.action)}
                  </span>
                  <span className="block font-mono text-xs text-slate-600">
                    {entry.entity_type}
                    {entry.entity_id ? ` · ${entry.entity_id.slice(0, 13)}` : ""}
                  </span>
                </span>
                <span className="shrink-0 rounded border border-hairline bg-raised px-1.5 py-px text-xs text-slate-400">
                  {entry.actor}
                </span>
                <span className="num w-14 shrink-0 text-right text-xs text-slate-600">
                  {timeAgo(entry.created_at, now)}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

export function DashboardPage({ refreshSignal }: { refreshSignal: number }) {
  const [summary, setSummary] = useState<DashboardSummary | null>(null);
  const [analytics, setAnalytics] = useState<AnalyticsOverview | null>(null);
  const [period, setPeriod] = useState<Period>(30);
  const [error, setError] = useState<string | null>(null);
  const [controllerRun, setControllerRun] = useState<LoopCloseReport | null>(null);
  const [controllerLoading, setControllerLoading] = useState<boolean>(false);
  const [controllerError, setControllerError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, a] = await Promise.all([
        api.dashboardSummary(),
        api.analytics({ from: isoDaysAgo(period) }),
      ]);
      setSummary(s);
      setAnalytics(a);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load dashboard");
    }
  }, [period]);

  const loadControllerRun = useCallback(async (ingestionId?: string) => {
    setControllerLoading(true);
    setControllerError(null);
    try {
      const report = await api.loopCloseReport({ ingestion_id: ingestionId });
      setControllerRun(report);
    } catch (err) {
      setControllerError(
        err instanceof Error ? err.message : "failed to load controller run"
      );
    } finally {
      setControllerLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    loadControllerRun();
  }, [load, loadControllerRun, refreshSignal]);

  if (error) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-critical/40 bg-critical/10 p-5 text-sm text-critical">
        <AlertIcon className="h-5 w-5 shrink-0" />
        {error}
      </div>
    );
  }

  const daily = analytics?.buckets ?? [];
  const decisionsDaily = daily.map((b) => b.auto_resolved + b.human_resolved + b.rejected);

  return (
    <div className="space-y-6">
      {/* Controller Run hero */}
      {controllerError && (
        <div className="flex items-center gap-3 rounded-xl border border-warning/40 bg-warning/10 p-4 text-sm text-warning">
          <AlertIcon className="h-5 w-5 shrink-0" />
          {controllerError}
        </div>
      )}
      {controllerLoading ? (
        <div className="card-interactive rounded-xl border border-hairline bg-surface p-6">
          <p className="microlabel">Running AI Finance Controller...</p>
        </div>
      ) : controllerRun ? (
        <div className="card-interactive rounded-xl border border-hairline bg-surface p-6">
          <header className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3 mb-4">
            <h2 className="microlabel">Controller Run</h2>
            <span className="text-xs text-slate-500">
              {controllerRun.scope?.ingestion_id
                ? `batch ${controllerRun.scope.ingestion_id}`
                : "—"}
            </span>
          </header>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <p className="microlabel">Closed</p>
              <p className={cn("num mt-2 text-[24px] font-semibold leading-none tracking-tight", "text-success")}>
                {controllerRun.matched} / {controllerRun.records_scanned}
              </p>
            </div>
            <div>
              <p className="microlabel">Match Rate</p>
              <p className={cn("num mt-2 text-[24px] font-semibold leading-none tracking-tight", "text-success")}>
                {(controllerRun.match_rate ?? 0) * 100}%
              </p>
            </div>
            <div>
              <p className="microlabel">Time</p>
              <p className={cn("num mt-2 text-[24px] font-semibold leading-none tracking-tight", "text-foreground")}>
                {controllerRun.execution_time_seconds ?? 0}s
              </p>
            </div>
            <div>
              <p className="microlabel">Deferred</p>
              <p className={cn("num mt-2 text-[24px] font-semibold leading-none tracking-tight", "text-warning")}>
                {controllerRun.deferred}
              </p>
            </div>
          </div>
          {controllerRun.accuracy_available ? (
            <div className="mt-4 p-3 border-l-4 border-success bg-success/10">
              <p className="microlabel text-sm text-success">Decision Accuracy</p>
              <p className="num mt-1 text-sm font-medium text-success">
                {roundToOneDecimal((controllerRun.decision_accuracy ?? 0) * 100)}%
              </p>
              <p className="text-xs text-success/80 mt-1">
                Recall: {roundToOneDecimal(
                  (controllerRun.matched_recall ?? 0) * 100
                )}%
              </p>
            </div>
          ) : (
            <p className="mt-2 text-sm text-slate-500">
              Ground-truth evaluation unavailable for this ingestion
            </p>
          )}
          <ExceptionsList exceptions={controllerRun.exceptions} />
          {controllerRun.scope?.ingestion_id && (
            <ExportButton ingestion_id={controllerRun.scope.ingestion_id} />
          )}
        </div>
      ) : (
        <p className="text-sm text-slate-500">No Controller Run Available</p>
      )}
      {/* Day-at-a-glance stat row */}
      {!summary ? (
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <StatCardSkeleton key={i} />
          ))}
        </div>
      ) : (
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-4">
          <StatCard
            label="Open exceptions"
            value={summary.open_exceptions_total}
            accent={summary.open_exceptions_total > 0 ? "text-warning" : "text-success"}
            subtle={
              PRIORITY_ORDER.filter((p) => summary.exceptions_by_priority[p]).length > 0 ? (
                <div className="flex flex-wrap gap-1.5 pt-1">
                  {PRIORITY_ORDER.filter((p) => summary.exceptions_by_priority[p]).map((p) => (
                    <span
                      key={p}
                      className={cn("num rounded border px-1.5 py-px text-[11px]", priorityChipClass(p))}
                    >
                      {summary.exceptions_by_priority[p]} {p}
                    </span>
                  ))}
                </div>
              ) : (
                "Nothing needs attention"
              )
            }
            spark={{
              points: daily.map((b) => b.exceptions_opened),
              color: "var(--color-warning)",
            }}
          />

          <StatCard
            label="Proposals awaiting review"
            value={summary.proposals_awaiting_review}
            accent={summary.proposals_awaiting_review > 0 ? "text-human" : "text-foreground"}
            subtle="Low-confidence AI matches & incomplete groups"
            spark={{
              points: daily.map((b) => b.matches_created),
              color: "var(--color-human)",
            }}
          />

          <StatCard
            label="Decisions today"
            value={summary.decisions_today_total}
            accent="text-success"
            subtle={
              <span className="num">
                auto <span className="text-slate-300">{summary.auto_resolved_today}</span>
                <span className="mx-1.5 text-slate-700">·</span>
                human <span className="text-slate-300">{summary.human_resolved_today}</span>
              </span>
            }
            spark={{ points: decisionsDaily, color: "var(--color-success)" }}
          />

          <StatCard
            label="Exceptions closed today"
            value={summary.exceptions_closed_today}
            accent="text-foreground"
            subtle="Resolved or dismissed"
            spark={{
              points: daily.map((b) => b.exceptions_resolved),
              color: "var(--color-success)",
            }}
          />
        </div>
      )}

      {/* Trends */}
      <section>
        <header className="mb-3 flex flex-wrap items-center justify-between gap-3 px-1">
          <h2 className="microlabel">Trends — last {period} days</h2>
          <div className="flex gap-1.5">
            {PERIODS.map((p) => (
              <button
                key={p}
                onClick={() => setPeriod(p)}
                className={cn(
                  "rounded-md border px-3 py-1.5 text-xs transition-colors",
                  period === p
                    ? "border-success/50 bg-success/15 text-success"
                    : "border-hairline bg-raised text-slate-400 hover:text-slate-200",
                )}
              >
                {p}d
              </button>
            ))}
          </div>
        </header>

        {!analytics ? (
          <ChartsSkeleton />
        ) : (
          <div className="grid gap-5 lg:grid-cols-3">
            <ChartCard
              title="Match decisions"
              context="Daily resolutions by authorship; dashed line = rejected proposals."
              className="lg:col-span-2"
            >
              <ResolutionFlowChart data={analytics.buckets} />
            </ChartCard>

            <ChartCard title="Auto vs human" context="Confirmed + rejected decisions in period.">
              <SplitDonut split={analytics.resolution_split} />
            </ChartCard>

            <ChartCard
              title="Exception flow"
              context="Opened vs closed per day."
              className="lg:col-span-2"
            >
              <ExceptionFlowChart data={analytics.buckets} />
            </ChartCard>

            <ChartCard title="By matcher" context="All matches created in period.">
              <MatcherBars breakdown={analytics.by_match_type} />
            </ChartCard>
          </div>
        )}
      </section>

{/* Open exception composition + live activity */}
      <section className="grid gap-5 lg:grid-cols-3">
        <div className="rounded-xl border border-hairline bg-surface lg:col-span-2">
          <header className="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-5 py-4">
            <h2 className="microlabel">Open exceptions by type</h2>
          </header>

          <div className="px-5 py-5">
            {!summary || Object.keys(summary.exceptions_by_type).length === 0 ? (
              <p className="py-6 text-center text-sm text-slate-500">
                No open exceptions — every record is reconciled or resolved.
              </p>
            ) : (
              <ul className="space-y-3">
                {Object.entries(summary.exceptions_by_type)
                  .sort(([, a], [, b]) => b - a)
                  .map(([etype, count]) => (
                    <li key={etype} className="flex items-center gap-4">
                      <span className="w-44 shrink-0 truncate text-[13px] capitalize text-slate-300">
                        {etype.replace(/_/g, " ")}
                      </span>
                      <div className="h-2 flex-1 overflow-hidden rounded-full bg-slate-800/70">
                        <div
                          className={cn(
                            "h-full rounded-full",
                            etype.includes("unmatched")
                              ? "bg-gradient-to-r from-rose-600 to-rose-500"
                              : etype.includes("review") || etype.includes("confidence")
                                ? "bg-gradient-to-r from-amber-600 to-amber-500"
                                : "bg-gradient-to-r from-slate-600 to-slate-500",
                          )}
                        />
                      </div>
                      <span className="num w-8 shrink-0 text-right text-sm font-medium text-slate-200">
                        {count}
                      </span>
                    </li>
                  ))}
              </ul>
            )}
          </div>
        </div>

        <ActivityFeed />
      </section>
    </div>
  );
}

function roundToOneDecimal(value: number | null | undefined): string {
  if (value === null || value === undefined) return "0";
  return Math.round(value * 10) / 10 + "";
}

function ExceptionsList({ exceptions }: { exceptions: LoopCloseException[] }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="card-interactive rounded-xl border border-hairline bg-surface p-5">
      <button
        onClick={() => setExpanded(!expanded)}
        className={cn(
          "w-full text-left text-sm text-slate-500 hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary flex items-center justify-between",
        )}
      >
       View Exceptions {exceptions.length} {expanded ? "▲" : "▼"}
      </button>
      {expanded && (
        <ul className="space-y-2 mt-3 max-h-40 overflow-y-auto">
          {exceptions.map((exc, i) => (
            <li key={i} className="flex items-start gap-3 px-3 py-2 border-b border-slate-800/30 last:border-0">
              <span className="w-14 shrink-0 text-[12px] text-slate-300 font-medium">
                {exc.record_ref ?? "—"}
              </span>
              <div className="flex-1">
                <p className="text-xs font-medium text-slate-200">Reason:</p>
                <p className="text-xs text-critical">{exc.reason_code}</p>
                <p className="text-xs font-medium text-slate-300">Count:</p>
                <p className="text-xs text-slate-300">{exc.count}</p>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ExportButton({ ingestion_id }: { ingestion_id: string }) {
  const handleDownload = async (format: "csv" | "pdf") => {
    try {
      await downloadReport(format, { ingestion_id });
    } catch (err) {
      console.error("Export failed", err);
    }
  };

  return (
    <Button
      variant="secondary"
      size="sm"
      onClick={() => handleDownload("csv")}
      className="flex items-center gap-1"
    >
      <FileTextIcon className="h-4 w-4" />
      Export CSV
    </Button>
  );
}