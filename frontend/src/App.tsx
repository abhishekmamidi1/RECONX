import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import {
  api,
  getActor,
  setActor,
  type HealthResponse,
  type SourceInfo,
  type SourceKey,
  type TransactionPage,
  type UploadResponse,
} from "@/api/client";
import { Badge, SOURCE_TONES } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { IngestionsHistory } from "@/components/IngestionsHistory";
import {
  ChevronRightIcon,
  InboxIcon,
  MenuIcon,
  RefreshIcon,
  UserIcon,
} from "@/components/ui/icons";
import { Skeleton, TableSkeleton } from "@/components/ui/skeleton";
import { ReviewDrawer } from "@/components/ReviewDrawer";
import { AutoResolvedDrawer } from "@/components/AutoResolvedDrawer";
import { CommandPalette, PaletteTrigger, type PaletteAction } from "@/components/CommandPalette";
import { ThemeToggle } from "@/components/ThemeToggle";
import { NAV_SECTIONS, SECTION_ICON, Sidebar, type SectionKey } from "@/components/Sidebar";
import { DashboardPage } from "@/pages/DashboardPage";
import { PolicyPage } from "@/pages/PolicyPage";
import { ReportsPage } from "@/pages/ReportsPage";
import { ResolvedPage } from "@/pages/ResolvedPage";
import { HumanReviewPage } from "@/pages/HumanReviewPage";
import { cn, formatDate, formatINR, isTypingTarget } from "@/lib/utils";

const SOURCE_OPTIONS: { key: SourceKey; label: string }[] = [
  { key: "razorpay", label: "Razorpay settlement" },
  { key: "bank", label: "Bank statement" },
  { key: "erp", label: "ERP ledger" },
];

const FIELD_CLASS =
  "w-full rounded-md border border-hairline bg-raised px-3 py-3.5 text-sm text-slate-200 placeholder:text-slate-600 transition-colors focus:border-green-500/70 focus:outline-none focus:ring-2 focus:ring-green-500/20";

function ActorField({ onActorChange }: { onActorChange: () => void }) {
  const [name, setName] = useState(getActor());

  function save(event: FormEvent) {
    event.preventDefault();
    setActor(name);
    onActorChange();
  }

  return (
    <form onSubmit={save} className="flex items-center gap-2">
      <div className="pointer-events-none flex h-8 w-8 items-center justify-center rounded-md border border-hairline bg-raised text-slate-500">
        <UserIcon className="h-4 w-4" />
      </div>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        onBlur={save}
        placeholder="actor id"
        title="Actions you take are audited under this identity"
        className="w-28 rounded-md border border-transparent bg-transparent px-2 py-1.5 text-sm text-slate-300 placeholder:text-slate-600 transition-colors hover:border-hairline focus:border-green-500/70 focus:bg-raised focus:outline-none"
      />
    </form>
  );
}

function StatusPills() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function poll() {
      try {
        const data = await api.health();
        if (!cancelled) {
          setHealth(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "unknown error");
      }
    }

    poll();
    const timer = setInterval(poll, 15000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  if (error) {
    return (
      <div className="flex items-center gap-2 rounded-full border border-critical/40 bg-critical/10 py-1 pl-2.5 pr-3">
        <span className="h-1.5 w-1.5 rounded-full bg-rose-500" />
        <span className="text-xs text-critical">API offline</span>
      </div>
    );
  }

  const connected = health?.database.connected ?? false;
  const ai = health?.ai;

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-2 rounded-full border border-hairline bg-surface py-1 pl-2.5 pr-3">
        <span className="relative flex h-1.5 w-1.5">
          {connected && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-green-400 opacity-60" />
          )}
          <span
            className={cn(
              "relative inline-flex h-1.5 w-1.5 rounded-full",
              connected ? "bg-green-400" : "bg-amber-400",
            )}
          />
        </span>
        <span className="text-xs text-slate-400">
          {!health ? (
            "connecting"
          ) : connected ? (
            <>
              db ok <span className="num text-slate-600">·</span>{" "}
              <span className="num text-slate-500">{health.database.latency_ms ?? "?"} ms</span>
            </>
          ) : (
            <span className="text-warning">db degraded</span>
          )}
        </span>
      </div>

      {health && ai && (
        <div
          className="flex items-center gap-2 rounded-full border border-hairline bg-surface py-1 pl-3 pr-3.5"
          title={`reasoning ${ai.reasoning_provider}/${ai.reasoning_model || "—"} · embeddings ${ai.embedding_provider}/${ai.embedding_model || "—"}`}
        >
          <span
            className={cn(
              "h-1.5 w-1.5 rounded-full",
              ai.reasoning_mode === "live" ? "bg-green-400" : "bg-amber-400",
            )}
          />
          <span className="text-xs text-slate-400">
            AI: <span className="text-slate-300">{ai.reasoning_provider}</span>{" "}
            <span className="num text-slate-600">·</span>{" "}
            <span className="num text-slate-300">{ai.reasoning_model || "—"}</span>
            {ai.reasoning_mode !== "live" && (
              <span className="ml-1.5 rounded border border-amber-500/40 bg-amber-500/10 px-1 py-px text-[11px] uppercase tracking-wide text-amber-400">
                fallback
              </span>
            )}
          </span>
        </div>
      )}
    </div>
  );
}

interface UploadResultState {
  kind: "success" | "error";
  message: string;
  detail?: UploadResponse;
}

function UploadCard({ onUploaded }: { onUploaded: () => void }) {
  const [source, setSource] = useState<SourceKey>("razorpay");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<UploadResultState | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (!file || busy) return;
    setBusy(true);
    setResult(null);
    try {
      const data = await api.uploadCsv(file, source);
      setResult({
        kind: data.rows_rejected > 0 ? "error" : "success",
        message:
          data.status === "completed"
            ? `Ingested ${data.filename}`
            : `Ingestion ${data.status}`,
        detail: data,
      });
      onUploaded();
    } catch (err) {
      setResult({
        kind: "error",
        message: err instanceof Error ? err.message : "upload failed",
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="rounded-xl border border-hairline bg-surface p-5">
      <h2 className="microlabel mb-4">Upload source file</h2>
      <form onSubmit={handleSubmit} className="space-y-4">
        <label className="block space-y-1.5">
          <span className="text-[13px] text-slate-400">Source type</span>
          <select
            value={source}
            onChange={(e) => setSource(e.target.value as SourceKey)}
            className={FIELD_CLASS}
          >
            {SOURCE_OPTIONS.map((option) => (
              <option key={option.key} value={option.key}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label className="block space-y-1.5">
          <span className="text-[13px] text-slate-400">CSV file</span>
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="w-full cursor-pointer rounded-lg border border-dashed border-slate-700 bg-raised/50 px-3 py-7 text-sm text-slate-400 transition-colors file:mr-3 file:rounded-md file:border-0 file:bg-slate-800 file:px-3 file:py-2 file:text-[13px] file:font-medium file:text-slate-200 hover:border-slate-600"
          />
        </label>

        <Button type="submit" disabled={!file || busy} className="w-full">
          {busy ? "Uploading…" : "Upload & parse"}
        </Button>
      </form>

      {result && (
        <div
          className={cn(
            "mt-4 rounded-lg border p-3",
            result.kind === "success"
              ? "border-success/40 bg-success/10"
              : "border-critical/40 bg-critical/10",
          )}
        >
          <p className={cn("text-sm font-medium", result.kind === "success" ? "text-success" : "text-critical")}>
            {result.message}
          </p>
          {result.detail && (
            <dl className="mt-2.5 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              <dt className="text-slate-500">rows parsed</dt>
              <dd className="num text-right text-slate-300">{result.detail.rows_total}</dd>
              <dt className="text-slate-500">inserted</dt>
              <dd className="num text-right text-success">{result.detail.rows_inserted}</dd>
              <dt className="text-slate-500">duplicates skipped</dt>
              <dd className="num text-right text-warning">{result.detail.rows_skipped_duplicate}</dd>
              <dt className="text-slate-500">rejected</dt>
              <dd className="num text-right text-critical">{result.detail.rows_rejected}</dd>
            </dl>
          )}
          {result.detail && result.detail.rejections_preview.length > 0 && (
            <ul className="mt-2 list-disc space-y-0.5 border-t border-critical/30 pt-2 pl-4 text-xs text-critical/90">
              {result.detail.rejections_preview.map((rejection) => (
                <li key={rejection}>{rejection}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

function SourcesCard() {
  const [sources, setSources] = useState<SourceInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .sources()
      .then(setSources)
      .catch((err) =>
        setError(err instanceof Error ? err.message : "failed to load sources"),
      );
  }, []);

  return (
    <section className="rounded-xl border border-hairline bg-surface p-5">
      <h2 className="microlabel mb-3">Supported formats</h2>
      {error && <p className="text-xs text-critical">{error}</p>}
      {!error && sources.length === 0 && <Skeleton className="h-16 w-full" />}
      <div className="space-y-1">
        {sources.map((source) => (
          <details key={source.key} className="group rounded-md px-1 py-1.5 hover:bg-gloss">
            <summary className="flex cursor-pointer list-none items-center marker:hidden">
              <span
                className={cn(
                  "h-1.5 w-1.5 shrink-0 rounded-full",
                  source.key === "razorpay"
                    ? "bg-source-razorpay"
                    : source.key === "bank"
                      ? "bg-source-bank"
                      : "bg-source-erp",
                )}
              />
              <span className="ml-2.5 text-sm text-slate-200">{source.label}</span>
              <span className="num ml-auto text-xs text-slate-600 group-open:hidden">
                {source.required_columns.length} cols
              </span>
            </summary>
            <p className="mt-1.5 pl-4 font-mono text-[11px] leading-relaxed text-slate-600">
              {source.required_columns.join(" · ")}
            </p>
          </details>
        ))}
      </div>
    </section>
  );
}

function TransactionsTable({ refreshSignal }: { refreshSignal: number }) {
  const [page, setPage] = useState<TransactionPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setPage(await api.transactions({ limit: 25 }));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load transactions");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  return (
    <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
      <header className="flex items-center justify-between border-b border-hairline px-5 py-4">
        <h2 className="microlabel">Recent transactions</h2>
        {page && (
          <span className="num rounded border border-hairline bg-raised px-1.5 py-px text-xs text-slate-500">
            {page.total} total
          </span>
        )}
      </header>

      {error && (
        <EmptyState icon={<InboxIcon className="h-5 w-5" />} title="Couldn't load transactions" hint={error} />
      )}

      {!error && !page && <TableSkeleton rows={8} columns={6} />}

      {!error && page && page.items.length === 0 && (
        <EmptyState
          icon={<InboxIcon className="h-5 w-5" />}
          title="No transactions yet"
          hint="Upload a Razorpay settlement, bank statement, or ERP export to populate the ledger."
        />
      )}

      {!error && page && page.items.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-hairline">
                <th className="microlabel px-5 py-3.5 font-medium">Reference</th>
                <th className="microlabel px-3 py-3.5 font-medium">Source</th>
                <th className="microlabel hidden px-3 py-3.5 font-medium md:table-cell">Date</th>
                <th className="microlabel px-3 py-3.5 text-right font-medium">Amount</th>
                <th className="microlabel px-3 py-3.5 font-medium">Flow</th>
                <th className="microlabel hidden px-5 py-3.5 font-medium lg:table-cell">Narration</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/40">
              {page.items.map((txn) => (
                <tr key={txn.id} className="group transition-colors hover:bg-gloss">
                  <td className="max-w-44 truncate px-5 py-3.5">
                    <span className="num text-[13px] text-slate-200">{txn.external_ref}</span>
                  </td>
                  <td className="px-3 py-3.5">
                    <Badge tone={SOURCE_TONES[txn.source]}>{txn.source}</Badge>
                  </td>
                  <td className="hidden whitespace-nowrap px-3 py-3.5 text-[13px] text-slate-500 md:table-cell">
                    {formatDate(txn.txn_date)}
                  </td>
                  <td className="whitespace-nowrap px-3 py-3.5 text-right">
                    <span className="num text-sm font-medium text-slate-200">
                      {formatINR(txn.amount)}
                    </span>
                  </td>
                  <td className="px-3 py-3.5">
                    <span
                      className={cn(
                        "inline-flex items-center gap-1 text-xs",
                        txn.direction === "credit" ? "text-success/90" : "text-critical/90",
                      )}
                      title={txn.direction}
                    >
                      <span aria-hidden>{txn.direction === "credit" ? "↓" : "↑"}</span>
                      <span className="capitalize">{txn.direction}</span>
                      {txn.transaction_type === "refund" && (
                        <span className="ml-1 rounded border border-critical/40 bg-critical/10 px-1 py-px text-[11px] uppercase tracking-wide text-critical">
                          refund
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="hidden max-w-64 truncate px-5 py-3.5 text-[13px] text-slate-500 lg:table-cell">
                    {txn.narration ?? <span className="text-slate-700">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export default function App() {
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [tab, setTab] = useState<SectionKey>("review");
  const [drawer, setDrawer] = useState<{ kind: "exception" | "proposal"; id: string } | null>(
    null,
  );
  const [resolvedMatch, setResolvedMatch] = useState<string | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("recon.sidebar") === "collapsed");
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  const bumpRefresh = useCallback(() => setRefreshSignal((n) => n + 1), []);

  const navigate = useCallback((next: SectionKey) => {
    setTab(next);
    setMobileNavOpen(false);
    window.scrollTo({ top: 0 });
  }, []);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      localStorage.setItem("recon.sidebar", next ? "collapsed" : "expanded");
      return next;
    });
  }, []);

  const runReconciliation = useCallback(async () => {
    try {
      await api.runReconciliation();
    } catch {
      // dashboard surfaces run failures on its own panel
    }
    navigate("dashboard");
    bumpRefresh();
  }, [navigate, bumpRefresh]);

  // Header "Run reconciliation" — the always-visible primary action. Spins
  // while running, reports the scan result on the button, then moves to the
  // dashboard where the fresh numbers load in.
  const [reconRunning, setReconRunning] = useState(false);
  const [reconResult, setReconResult] = useState<{ ok: boolean; text: string } | null>(null);
  const reconTimer = useRef<number | undefined>(undefined);

  const runFromHeader = useCallback(async () => {
    if (reconRunning) return;
    setReconRunning(true);
    setReconResult(null);
    try {
      const result = await api.runReconciliation();
      setReconResult({
        ok: true,
        text: `${result.transactions_scanned.toLocaleString("en-IN")} transactions · ${Number(result.duration_ms).toFixed(0)} ms`,
      });
    } catch (err) {
      setReconResult({ ok: false, text: err instanceof Error ? err.message : "run failed" });
    } finally {
      setReconRunning(false);
      navigate("dashboard");
      bumpRefresh();
      window.clearTimeout(reconTimer.current);
      reconTimer.current = window.setTimeout(() => setReconResult(null), 4000);
    }
  }, [reconRunning, navigate, bumpRefresh]);

  // ⌘K / Ctrl+K opens the palette from anywhere; "/" too when not typing.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((open) => !open);
        return;
      }
      if (event.key === "/" && !isTypingTarget(event.target)) {
        event.preventDefault();
        setPaletteOpen(true);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  const currentSection = NAV_SECTIONS.find((s) => s.key === tab);
  const sectionLabel = currentSection?.label ?? "RECONX";

  const paletteActions = useMemo<{ group: string; items: PaletteAction[] }[]>(
    () => [
      {
        group: "Navigate",
        items: [
          { id: "go-dashboard", label: "Go to Dashboard", hint: "Overview, trends, activity", keywords: ["overview"], icon: SECTION_ICON.dashboard, run: () => navigate("dashboard") },
          { id: "go-review", label: "Go to Human Review", hint: "Exceptions & proposals awaiting a human decision", icon: SECTION_ICON.review, run: () => navigate("review") },
          { id: "go-resolved", label: "Go to Auto-Resolved", hint: "Matches the pipeline settled on its own, by stage", icon: SECTION_ICON.resolved, run: () => navigate("resolved") },
          { id: "go-policy", label: "Go to Policy", hint: "Matching thresholds & auto-resolve gates", icon: SECTION_ICON.policy, run: () => navigate("policy") },
          { id: "go-reports", label: "Go to Reports", hint: "CSV / PDF exports & ERP webhook", icon: SECTION_ICON.reports, run: () => navigate("reports") },
          { id: "go-ingest", label: "Go to Data sources", hint: "Upload a statement or settlement", icon: SECTION_ICON.ingest, run: () => navigate("ingest") },
        ],
      },
      {
        group: "Actions",
        items: [
          { id: "run-recon", label: "Run reconciliation", hint: "Scan all records, propose & auto-resolve", keywords: ["scan", "match"], icon: <RefreshIcon className="h-3.5 w-3.5" />, run: runReconciliation },
          { id: "to-reports", label: "Export a report", hint: "Open Reports to download CSV or PDF", keywords: ["csv", "pdf", "export"], icon: SECTION_ICON.reports, run: () => navigate("reports") },
        ],
      },
    ],
    [navigate, runReconciliation],
  );

  return (
    <div className="flex min-h-screen">
      <Sidebar
        active={tab}
        onNavigate={navigate}
        collapsed={collapsed}
        onToggleCollapsed={toggleCollapsed}
        mobileOpen={mobileNavOpen}
        onCloseMobile={() => setMobileNavOpen(false)}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 border-b border-hairline bg-canvas/85 backdrop-blur-md">
          <div className="mx-auto flex h-16 max-w-7xl items-center justify-between gap-4 px-6">
            <div className="flex min-w-0 items-center gap-3">
              <Button
                className="lg:hidden"
                variant="ghost"
                size="sm"
                onClick={() => setMobileNavOpen(true)}
                aria-label="Open navigation"
              >
                <MenuIcon className="h-4 w-4" />
              </Button>
              <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-1.5 text-sm">
                <span className="shrink-0 text-slate-500">RECONX</span>
                <ChevronRightIcon className="h-3.5 w-3.5 shrink-0 text-slate-600" />
                <span className="truncate font-display font-medium capitalize text-foreground">{sectionLabel}</span>
              </nav>
            </div>
            <div className="flex items-center gap-2.5">
              <Button
                onClick={runFromHeader}
                disabled={reconRunning}
                size="sm"
                className="min-w-40"
                title="Scan every record, propose matches, and auto-resolve what clears the gates"
              >
                <RefreshIcon className={cn("h-3.5 w-3.5", reconRunning && "animate-spin")} />
                {reconRunning ? "Running…" : "Run reconciliation"}
              </Button>
              {reconResult && (
                <span
                  role="status"
                  className={cn(
                    "animate-slides-down hidden max-w-56 truncate rounded-md border px-2.5 py-1 text-xs md:inline-block",
                    reconResult.ok
                      ? "border-success/40 bg-success/10 text-success"
                      : "border-critical/40 bg-critical/10 text-critical",
                  )}
                >
                  {reconResult.ok && "✓ "}
                  {reconResult.text}
                </span>
              )}
              <PaletteTrigger onClick={() => setPaletteOpen(true)} />
              <ThemeToggle />
              <ActorField onActorChange={bumpRefresh} />
              <div className="h-5 w-px bg-hairline" />
              <StatusPills />
            </div>
          </div>
        </header>

        <main className="mx-auto w-full max-w-7xl flex-1 px-6 py-8">
          <div key={tab} className="animate-fade-up space-y-5">
            {tab === "dashboard" && <DashboardPage refreshSignal={refreshSignal} />}
            {tab === "policy" && <PolicyPage refreshSignal={refreshSignal} />}
            {tab === "reports" && <ReportsPage refreshSignal={refreshSignal} />}
            {tab === "review" && (
              <HumanReviewPage
                refreshSignal={refreshSignal}
                onOpenItem={(kind, id) => setDrawer({ kind, id })}
              />
            )}
            {tab === "resolved" && (
              <ResolvedPage refreshSignal={refreshSignal} onOpenMatch={setResolvedMatch} />
            )}
            {tab === "ingest" && (
              <div className="space-y-5">
                <div className="grid gap-5 lg:grid-cols-[360px_1fr]">
                  <div className="space-y-5">
                    <UploadCard onUploaded={bumpRefresh} />
                    <SourcesCard />
                  </div>
                  <TransactionsTable refreshSignal={refreshSignal} />
                </div>
                <IngestionsHistory refreshSignal={refreshSignal} />
              </div>
            )}
          </div>
        </main>
      </div>

      {drawer && (
        <ReviewDrawer
          target={drawer}
          onClose={() => setDrawer(null)}
          onActionCompleted={bumpRefresh}
        />
      )}

      {resolvedMatch && (
        <AutoResolvedDrawer matchId={resolvedMatch} onClose={() => setResolvedMatch(null)} />
      )}

      <CommandPalette
        open={paletteOpen}
        actions={paletteActions}
        onClose={() => setPaletteOpen(false)}
      />
    </div>
  );
}