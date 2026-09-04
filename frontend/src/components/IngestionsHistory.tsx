import { useCallback, useEffect, useState } from "react";

import { api, downloadReport, type IngestionRecord } from "@/api/client";
import { Badge, SOURCE_TONES } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { FileTextIcon, InboxIcon, RefreshIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatDateTime } from "@/lib/utils";

function SourceBadge({ source }: { source: IngestionRecord["source"] }) {
  return <Badge tone={SOURCE_TONES[source]}>{source}</Badge>;
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <span className="text-xs text-slate-500" title={label}>
      <span className="mr-1 text-xs text-slate-700">{label}</span>
      <span className={cn("num", tone)}>{value}</span>
    </span>
  );
}

/**
 * Past uploads with one-click CSV / PDF export per batch. Each ingestion is a
 * report scope: summary stats are restricted to that batch's own transactions
 * while matches that also touch other batches keep full context.
 */
export function IngestionsHistory({ refreshSignal }: { refreshSignal: number }) {
  const [ingestions, setIngestions] = useState<IngestionRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<{ key: string; format: "csv" | "pdf" } | null>(null);
  const [flash, setFlash] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setIngestions(await api.ingestions());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load ingestion history");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  async function exportBatch(ingestion: IngestionRecord, format: "csv" | "pdf") {
    setBusy({ key: ingestion.id, format });
    setFlash(null);
    try {
      const result = await downloadReport(format, { ingestion_id: ingestion.id });
      setFlash({
        ok: true,
        text: `${result.filename} downloaded (${(result.size / 1024).toFixed(1)} kB)`,
      });
    } catch (err) {
      setFlash({ ok: false, text: err instanceof Error ? err.message : "export failed" });
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-hairline px-5 py-3.5">
        <div>
          <h2 className="microlabel">Ingestion history</h2>
          <p className="mt-1 text-xs text-slate-500">
            Every uploaded batch is its own report scope — export CSV or PDF to see only that
            batch's transactions and how they matched.
          </p>
        </div>
        <Button onClick={load} variant="secondary" size="sm">
          <RefreshIcon className="h-3 w-3" />
          Refresh
        </Button>
      </header>

      {flash && (
        <p
          className={cn(
            "flex items-center border-b border-hairline px-5 py-2 text-xs",
            flash.ok ? "bg-success/5 text-success" : "bg-critical/5 text-critical",
          )}
        >
          {flash.text}
        </p>
      )}

      {error && (
        <EmptyState
          icon={<InboxIcon className="h-5 w-5" />}
          title="Couldn't load ingestion history"
          hint={error}
        />
      )}

      {!error && !ingestions && (
        <div className="space-y-2 px-5 py-4">
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-12 w-full" />
          ))}
        </div>
      )}

      {!error && ingestions && ingestions.length === 0 && (
        <EmptyState
          icon={<InboxIcon className="h-5 w-5" />}
          title="No uploads yet"
          hint="Upload a CSV above and it will appear here with export actions."
        />
      )}

      {!error && ingestions && ingestions.length > 0 && (
        <ul className="divide-y divide-slate-800/40">
          {ingestions.map((ingestion) => (
            <li
              key={ingestion.id}
              className="flex flex-wrap items-center gap-x-4 gap-y-2 px-5 py-3.5"
            >
              <div className="min-w-0 flex-1 basis-64">
                <p className="truncate font-mono text-sm text-slate-200" title={ingestion.id}>
                  {ingestion.filename}
                </p>
                <p className="mt-0.5 flex items-center gap-2">
                  <SourceBadge source={ingestion.source} />
                  <span className="text-xs text-slate-600">{formatDateTime(ingestion.created_at)}</span>
                </p>
              </div>

              <div className="flex items-center gap-3">
                <Stat label="read" value={ingestion.rows_total} />
                <Stat label="inserted" value={ingestion.rows_inserted} tone="text-success" />
                {ingestion.rows_skipped_duplicate > 0 && (
                  <Stat label="dup" value={ingestion.rows_skipped_duplicate} tone="text-warning" />
                )}
                {ingestion.rows_rejected > 0 && (
                  <Stat label="rejected" value={ingestion.rows_rejected} tone="text-critical" />
                )}
              </div>

              <Badge
                tone={ingestion.status === "completed" ? "success" : ingestion.status === "failed" ? "danger" : "warning"}
              >
                {ingestion.status}
              </Badge>
              {ingestion.error_detail && (
                <span className="max-w-52 truncate text-xs text-critical/80" title={ingestion.error_detail}>
                  {ingestion.error_detail}
                </span>
              )}

              <span className="flex gap-1.5">
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={busy?.key === ingestion.id}
                  onClick={() => exportBatch(ingestion, "csv")}
                >
                  {busy?.key === ingestion.id && busy.format === "csv" ? (
                    "Generating…"
                  ) : (
                    <>
                      <FileTextIcon className="h-3 w-3" />
                      CSV
                    </>
                  )}
                </Button>
                <Button
                  size="sm"
                  disabled={busy?.key === ingestion.id}
                  onClick={() => exportBatch(ingestion, "pdf")}
                >
                  {busy?.key === ingestion.id && busy.format === "pdf" ? (
                    "Generating…"
                  ) : (
                    <>
                      <FileTextIcon className="h-3 w-3" />
                      PDF
                    </>
                  )}
                </Button>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}