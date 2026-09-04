import { Badge, SOURCE_TONES } from "@/components/ui/badge";
import { formatDate, formatINR } from "@/lib/utils";
import type { TxnDetail } from "@/api/client";

export function SectionLabel({ children }: { children: React.ReactNode }) {
  return <h3 className="microlabel mb-2.5">{children}</h3>;
}

export function RecordCard({
  txn,
  highlight,
}: {
  txn: TxnDetail;
  highlight?: boolean;
}) {
  if (!txn) return null;
  const rawEntries = Object.entries(txn.raw ?? {}).filter(
    ([, v]) => v !== null && v !== "" && typeof v !== "object",
  );
  return (
    <div
      className={
        "flex flex-col rounded-xl border bg-raised/60 p-4 " +
        (highlight ? "border-success/40" : "border-hairline")
      }
    >
      <div className="mb-3 flex items-center justify-between gap-2">
        <div className="flex items-center gap-1.5">
          <Badge tone={SOURCE_TONES[txn.source] ?? "neutral"}>{txn.source}</Badge>
          {txn.transaction_type === "refund" && (
            <span className="rounded border border-critical/40 bg-critical/10 px-1 py-px text-[11px] uppercase tracking-wide text-critical">
              refund
            </span>
          )}
        </div>
        <span
          className={
            "num text-sm font-semibold tracking-tight " +
            (txn.direction === "credit" ? "text-success" : "text-critical")
          }
        >
          {formatINR(txn.amount)}
        </span>
      </div>

      <p className="num text-[13px] leading-snug text-slate-200">{txn.external_ref}</p>

      <dl className="mt-2.5 space-y-1.5 border-t border-slate-800/70 pt-2.5 text-xs">
        <div className="flex items-baseline justify-between gap-3">
          <dt className="text-slate-600">date</dt>
          <dd className="text-slate-300">{formatDate(txn.txn_date)}</dd>
        </div>
        {txn.counterparty && (
          <div className="flex items-baseline justify-between gap-3">
            <dt className="shrink-0 text-slate-600">party</dt>
            <dd className="max-w-44 truncate text-right text-slate-300">{txn.counterparty}</dd>
          </div>
        )}
        {txn.status && (
          <div className="flex items-baseline justify-between gap-3">
            <dt className="text-slate-600">status</dt>
            <dd className="capitalize text-slate-300">{txn.status}</dd>
          </div>
        )}
      </dl>

      {txn.narration && (
        <p className="mt-2 rounded-md bg-gloss px-2.5 py-1.5 text-xs italic leading-relaxed text-slate-500">
          “{txn.narration}”
        </p>
      )}

      {rawEntries.length > 0 && (
        <details className="mt-auto pt-2.5">
          <summary className="cursor-pointer select-none text-[11px] font-medium uppercase tracking-[0.08em] text-slate-600 transition-colors hover:text-slate-400 marker:hidden">
            Raw payload · {rawEntries.length} fields
          </summary>
          <dl className="mt-1.5 max-h-40 space-y-0.5 overflow-y-auto border-t border-slate-800/70 pt-1.5">
            {rawEntries.map(([key, value]) => (
              <div key={key} className="flex items-baseline justify-between gap-3">
                <dt className="shrink-0 font-mono text-[11px] text-slate-600">{key}</dt>
                <dd className="max-w-44 break-all text-right font-mono text-[11px] text-slate-500">
                  {String(value)}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      )}
    </div>
  );
}
