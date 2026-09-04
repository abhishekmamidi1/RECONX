import { useCallback, useEffect, useState } from "react";

import {
  api,
  type MatchDetail,
  type Recommendation,
  type TxnDetail,
} from "@/api/client";
import { Badge, matchTypeTone, statusTone, type Tone } from "@/components/ui/badge";
import { ConfidenceMeter } from "@/components/ui/confidence";
import { RecordCard, SectionLabel } from "@/components/RecordCard";
import { CheckCircleIcon, SparklesIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatDateTime, formatINR } from "@/lib/utils";

const STAGE_TONE: Record<string, "neutral" | "ai" | "info" | "success"> = {
  deterministic: "success",
  fuzzy: "info",
  semantic: "ai",
  ai: "ai",
};

interface ExactEdge {
  a: string;
  b: string;
  field: string;
  value: string;
}

/** Derive the exact identity field(s) that linked each source pair, using
 *  only data already present in the participants' payloads. Mirrors the
 *  deterministic pass: razorpay→erp on payment_ref, razorpay→bank on UTR. */
function deriveExactMatches(participants: TxnDetail[]): ExactEdge[] {
  const edges: ExactEdge[] = [];
  for (let i = 0; i < participants.length; i++) {
    for (let j = i + 1; j < participants.length; j++) {
      const a = participants[i];
      const b = participants[j];
      const pair = [a.source, b.source].sort().join("/");

      let field: string | null = null;
      let value: string | null = null;
      if (pair === "erp/razorpay") {
        const rz = a.source === "razorpay" ? a : b;
        const erp = a.source === "erp" ? a : b;
        const erpPayRef = String(erp.raw?.payment_ref ?? "");
        if (erpPayRef && erpPayRef === rz.external_ref) {
          field = "payment_ref";
          value = erpPayRef;
        }
      } else if (pair === "bank/razorpay") {
        const rz = a.source === "razorpay" ? a : b;
        const bank = a.source === "bank" ? a : b;
        const rzUtr = String(rz.raw?.utr ?? "");
        const bankRef = bank.external_ref;
        const bankRawRef = String(bank.raw?.ref_no ?? "");
        const bankVal = bankRef || bankRawRef;
        if (rzUtr && bankVal && (rzUtr === bankRef || rzUtr === bankRawRef)) {
          field = "UTRs";
          value = rzUtr;
        }
      }

      if (field && value) edges.push({ a: a.source, b: b.source, field, value });
    }
  }
  return edges;
}

/** The two fuzzy-compared identity strings, for the side-by-side. Prefers
 *  the near-miss UTR pair (razorpay.utr ↔ bank ref), because that is what the
 *  fuzzy pass actually scored; falls back to any two refs. */
function fuzzyComparison(match: MatchDetail): { left?: string; right?: string } {
  const parts = match.participants;
  const rz = parts.find((p) => p.source === "razorpay");
  const bank = parts.find((p) => p.source === "bank");
  if (rz && bank) {
    const rzUtr = String(rz.raw?.utr ?? "");
    const bankRef = bank.external_ref || String(bank.raw?.ref_no ?? "");
    if (rzUtr && bankRef) return { left: rzUtr, right: bankRef };
  }
  const nonBank = parts.filter((p) => p.source !== "bank");
  if (bank && nonBank.length) {
    return { left: nonBank[0].external_ref, right: bank.external_ref };
  }
  const narrationA = parts[0]?.narration ?? parts[0]?.external_ref;
  const narrationB = parts[1]?.narration ?? parts[1]?.external_ref;
  return { left: narrationA, right: narrationB };
}

/** Identical visual treatment to the Human Review drawer's AI recommendation
 *  card — same tokens, same layout — framed for a case that auto-resolved. */
function AutoAiCard({ rec, confidence }: { rec: Recommendation; confidence: string }) {
  const verdict: { label: string; tone: Tone } | null =
    rec.verdict === "match" ? { label: "match", tone: "success" } : null;

  const simLine =
    rec.similarity != null && rec.similarity_autoresolve_min != null
      ? rec.floor_met
        ? `similarity ${rec.similarity.toFixed(3)} — cleared the ${rec.similarity_autoresolve_min.toFixed(
            3,
          )} auto-resolve floor`
        : `similarity ${rec.similarity.toFixed(3)} — below the ${rec.similarity_autoresolve_min.toFixed(
            3,
          )} auto-resolve floor`
      : rec.similarity != null
        ? `similarity ${rec.similarity.toFixed(3)}`
        : null;

  const confLine =
    rec.confidence_autoresolve_min != null
      ? rec.confidence_floor_met
        ? `cleared the ${rec.confidence_autoresolve_min.toFixed(2)} min-confidence gate`
        : `below the ${rec.confidence_autoresolve_min.toFixed(2)} min-confidence gate`
      : null;

  return (
    <section className="relative overflow-hidden rounded-xl border border-human/30 bg-human/10 p-5 pl-12">
      <SparklesIcon className="absolute left-4 top-5 h-4 w-4 text-human" />
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-human">
          AI resolution
        </p>
        {verdict && <Badge tone={verdict.tone}>{verdict.label}</Badge>}
        <Badge tone="ai">{rec.stage}</Badge>
      </div>

      <p className="mt-2 text-sm font-light leading-relaxed text-human-ink">
        The model matched these records and the joint gate let it auto-resolve — no human needed.
      </p>

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <div className="flex items-center justify-between rounded-lg border border-human/20 bg-raised/60 px-3.5 py-2.5">
          <span className="microlabel">Confidence</span>
          <div className="flex items-center gap-2">
            <ConfidenceMeter value={confidence} />
            {confLine && (
              <span
                className={cn(
                  "num text-right text-[11px] leading-tight",
                  rec.confidence_floor_met ? "text-success" : "text-critical",
                )}
                title="vs. gate.ai_min_confidence_autoresolve"
              >
                {rec.confidence_floor_met ? "gate ✓" : "gate ✗"}
              </span>
            )}
          </div>
        </div>
        {simLine && (
          <div
            className="flex items-center justify-between gap-2 rounded-lg border border-human/20 bg-raised/60 px-3.5 py-2.5"
            title="semantic similarity vs. matching.ai.similarity_autoresolve_min"
          >
            <span className="microlabel">Similarity</span>
            <span
              className={cn(
                "num text-right text-[11px] leading-tight",
                rec.floor_met ? "text-success" : "text-critical",
              )}
            >
              {simLine}
            </span>
          </div>
        )}
      </div>

      <p className="mt-3 text-[11px] font-semibold uppercase tracking-[0.1em] text-human/70">
        Why the model paired them
      </p>
      <blockquote className="mt-1 border-l-2 border-human/50 pl-3 text-sm font-light italic leading-relaxed text-human-ink">
        {rec.rationale ?? "No rationale recorded."}
      </blockquote>

      {rec.confidence_autoresolve_min != null && confLine && (
        <p className="mt-3 rounded-lg border border-success/25 bg-success/5 px-3.5 py-2.5 text-xs text-success">
          Auto-resolved because it passed both gates: confidence {Number(confidence).toFixed(2)} ≥{" "}
          {rec.confidence_autoresolve_min.toFixed(2)} and similarity{" "}
          {rec.similarity?.toFixed(3)} ≥ {rec.similarity_autoresolve_min?.toFixed(3)}.
        </p>
      )}
    </section>
  );
}

export function AutoResolvedDrawer({
  matchId,
  onClose,
}: {
  matchId: string;
  onClose: () => void;
}) {
  const [match, setMatch] = useState<MatchDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setMatch(await api.matchDetail(matchId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load detail");
    } finally {
      setLoading(false);
    }
  }, [matchId]);

  useEffect(() => {
    load();
  }, [load]);

  const rec = match?.recommendation ?? null;
  const stage = match?.match_type ?? null;
  const tone = STAGE_TONE[stage ?? ""] ?? "neutral";

  const exactEdges = match && match.match_type === "deterministic" ? deriveExactMatches(match.participants) : [];
  const fuzzyPair = match && match.match_type === "fuzzy" ? fuzzyComparison(match) : {};
  const participants = match?.participants ?? [];
  const total = participants.reduce((a, p) => a + Number(p.amount), 0);

  return (
    <div className="fixed inset-0 z-30 flex justify-end">
      <button
        aria-label="close drawer"
        onClick={onClose}
        className="absolute inset-0 bg-overlay backdrop-blur-[2px]"
      />
      <aside className="animate-drawer-in relative flex h-full w-full max-w-2xl flex-col border-l border-hairline bg-canvas shadow-[-24px_0_48px_rgba(0,0,0,0.45)]">
        {/* ── Header ─────────────────────────────────────────── */}
        <header className="border-b border-hairline bg-surface/80 px-6 py-4 backdrop-blur">
          <div className="flex items-start justify-between gap-3">
            {loading ? (
              <div className="space-y-2">
                <Skeleton className="h-3 w-36" />
                <Skeleton className="h-2 w-24" />
              </div>
            ) : (
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge tone={matchTypeTone(match?.match_type ?? null)}>{match?.match_type}</Badge>
                  <Badge tone={tone}>{stage}</Badge>
                  <Badge tone={statusTone(match?.status ?? "")}>
                    {(match?.status ?? "").replace(/_/g, " ")}
                  </Badge>
                  <span className="flex items-center gap-1 rounded-full border border-success/30 bg-success/10 px-2 py-0.5 text-[11px] font-medium text-success">
                    <CheckCircleIcon className="h-3 w-3" />
                    auto-resolved
                  </span>
                </div>
                <h2 className="mt-1.5 truncate text-[17px] font-semibold capitalize tracking-tight text-foreground">
                  {stage} resolution
                </h2>
                <p className="num mt-0.5 truncate text-xs text-slate-500">
                  {participants.map((p) => p.external_ref).join(" · ")}
                </p>
              </div>
            )}
            <button
              onClick={onClose}
              className="-mr-1 -mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-500 transition-colors hover:bg-gloss hover:text-slate-200"
              aria-label="Close"
            >
              ✕
            </button>
          </div>
        </header>

        {/* ── Body ───────────────────────────────────────────── */}
        <div className="flex-1 space-y-7 overflow-y-auto px-6 py-6">
          {loading && (
            <div className="space-y-4">
              <Skeleton className="h-28 w-full" />
              <div className="grid grid-cols-2 gap-3">
                <Skeleton className="h-44" />
                <Skeleton className="h-44" />
              </div>
            </div>
          )}

          {!loading && error && (
            <div className="rounded-lg border border-critical/40 bg-critical/10 px-3 py-2.5 text-xs text-critical">
              {error}
            </div>
          )}

          {!loading && match && (
            <>
              {/* Overview: confidence + timestamps */}
              <section className="rounded-xl border border-hairline bg-surface p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-slate-400">
                    How it was resolved
                  </p>
                  <span className="num text-sm font-semibold tracking-tight text-slate-200">
                    {formatINR(total)}
                  </span>
                </div>
                <div className="mt-3 grid gap-3 sm:grid-cols-2">
                  <div className="flex items-center justify-between rounded-lg border border-hairline bg-raised/50 px-3.5 py-2.5">
                    <span className="microlabel">Confidence</span>
                    <ConfidenceMeter value={match.confidence_score} />
                  </div>
                  <div className="flex items-center justify-between gap-2 rounded-lg border border-hairline bg-raised/50 px-3.5 py-2.5">
                    <span className="microlabel">Stage</span>
                    <Badge tone={tone}>{stage}</Badge>
                  </div>
                </div>
                <dl className="mt-3 space-y-1 border-t border-slate-800/70 pt-3 text-xs">
                  <div className="flex items-baseline justify-between gap-3">
                    <dt className="text-slate-600">proposed</dt>
                    <dd className="num text-slate-300">{formatDateTime(match.proposed_at)}</dd>
                  </div>
                  <div className="flex items-baseline justify-between gap-3">
                    <dt className="text-slate-600">auto-resolved</dt>
                    <dd className="num text-success">
                      {formatDateTime(match.resolved_at ?? match.proposed_at)}
                    </dd>
                  </div>
                </dl>
              </section>

              {/* Stage-specific transparency */}
              {stage === "deterministic" && (
                <section className="rounded-xl border border-hairline bg-surface p-4">
                  <SectionLabel>What matched — exactly</SectionLabel>
                  {exactEdges.length > 0 ? (
                    <ul className="space-y-2">
                      {exactEdges.map((edge, i) => (
                        <li
                          key={i}
                          className="flex flex-wrap items-center gap-2 rounded-lg border border-success/25 bg-success/5 px-3 py-2 text-xs"
                        >
                          <CheckCircleIcon className="h-3.5 w-3.5 shrink-0 text-success" />
                          <span className="text-slate-300">
                            exact <span className="font-medium text-success">{edge.field}</span>{" "}
                            across <span className="text-slate-300">{edge.a}</span>/
                            <span className="text-slate-300">{edge.b}</span>
                          </span>
                          <code className="num ml-auto max-w-52 truncate rounded bg-gloss px-1.5 py-0.5 text-xs text-slate-400">
                            {edge.value}
                          </code>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-xs text-slate-500">{match.rationale}</p>
                  )}
                  <p className="mt-3 text-xs leading-relaxed text-slate-500">
                    Every identity field aligned with zero ambiguity, so the pipeline auto-resolved
                    the group without a human. {match.rationale}
                  </p>
                </section>
              )}

              {stage === "fuzzy" && (
                <section className="rounded-xl border border-hairline bg-surface p-4">
                  <SectionLabel>What fuzzy compared</SectionLabel>
                  <p className="text-xs leading-relaxed text-slate-500">
                    Some identity field(s) matched closely but not exactly, so the fuzzy pass scored
                    them on reference/narration closeness and auto-resolved once the score cleared
                    the fuzzy threshold.
                  </p>

                  {fuzzyPair.left && fuzzyPair.right && (
                    <div className="mt-3 rounded-lg border border-hairline bg-raised/40 p-3">
                      <p className="microlabel mb-2">Reference strings compared</p>
                      <div className="flex items-center gap-2">
                        <code className="num min-w-0 flex-1 truncate rounded bg-gloss px-2 py-2 text-sm text-slate-200">
                          {fuzzyPair.left}
                        </code>
                        <span className="text-slate-600">↔</span>
                        <code className="num min-w-0 flex-1 truncate rounded bg-gloss px-2 py-2 text-sm text-slate-200">
                          {fuzzyPair.right}
                        </code>
                      </div>
                    </div>
                  )}

                  <div className="mt-3 grid gap-2 sm:grid-cols-2">
                    <div className="flex items-center justify-between rounded-lg border border-hairline bg-raised/50 px-3.5 py-2.5">
                      <span className="microlabel">Similarity</span>
                      <ConfidenceMeter value={match.confidence_score} />
                    </div>
                    <div className="flex items-center justify-between rounded-lg border border-hairline bg-raised/50 px-3.5 py-2.5">
                      <span className="microlabel">Result</span>
                      <span className="text-xs font-medium text-success">auto-resolved</span>
                    </div>
                  </div>

                  {match.rationale && (
                    <p className="mt-3 text-xs leading-relaxed text-slate-500">{match.rationale}</p>
                  )}
                </section>
              )}

              {stage === "semantic" && rec && (
                <section className="rounded-xl border border-hairline bg-surface p-4">
                  <SectionLabel>Semantic match</SectionLabel>
                  <p className="text-xs leading-relaxed text-slate-500">
                    The records were embedded and compared on meaning — narration and reference text —
                    scoring {rec.similarity?.toFixed(3) ?? "—"} against a{" "}
                    {rec.similarity_autoresolve_min?.toFixed(3) ?? "the"} similarity threshold to
                    auto-resolve.
                  </p>
                  <div className="mt-3 flex items-center justify-between rounded-lg border border-hairline bg-raised/50 px-3.5 py-2.5">
                    <span className="microlabel">Similarity</span>
                    <ConfidenceMeter value={rec.similarity ?? Number(match.confidence_score)} />
                  </div>
                  {match.rationale && (
                    <p className="mt-3 text-xs leading-relaxed text-slate-500">{match.rationale}</p>
                  )}
                </section>
              )}

              {stage === "ai" && rec && <AutoAiCard rec={rec} confidence={match.confidence_score} />}
            </>
          )}

          {/* Side-by-side records — same treatment as the review drawer */}
          {!loading && match && match.participants.length > 0 && (
            <section>
              <SectionLabel>Records involved</SectionLabel>
              <div className="grid gap-3 sm:grid-cols-2">
                {match.participants.map((p) => (
                  <RecordCard key={p.id} txn={p} />
                ))}
              </div>
            </section>
          )}
        </div>

        <footer className="border-t border-hairline bg-surface/90 px-6 py-3.5 backdrop-blur">
          <p className="text-center text-xs text-slate-600">
            Read-only — this case is already resolved. Open the Human Review tab to see pending
            items you can still act on.
          </p>
        </footer>
      </aside>
    </div>
  );
}
