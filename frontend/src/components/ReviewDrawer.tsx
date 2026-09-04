import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  api,
  type Analysis,
  type Candidate,
  type ExceptionDetail,
  type MatchDetail,
  type Recommendation,
} from "@/api/client";
import { Badge, matchTypeTone, priorityTone, SOURCE_TONES, statusTone, type Tone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfidenceMeter } from "@/components/ui/confidence";
import { ArrowRightIcon, CheckIcon, SearchIcon, SparklesIcon, XIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import { RecordCard, SectionLabel } from "@/components/RecordCard";
import { cn, formatDate, formatINR } from "@/lib/utils";

const FIELD_CLASS =
  "w-full rounded-md border border-hairline bg-raised px-3.5 py-2.5 text-sm text-slate-200 placeholder:text-slate-600 transition-colors focus:border-green-500/70 focus:outline-none focus:ring-2 focus:ring-green-500/20";

interface DrawerState {
  kind: "exception" | "proposal";
  id: string;
}

const VERDICTS: Record<string, { label: string; tone: Tone }> = {
  needs_human: { label: "needs human review", tone: "warning" },
  match: { label: "match", tone: "success" },
  no_match: { label: "no match", tone: "neutral" },
};

const ANALYSIS_KINDS: Record<string, { label: string; tone: Tone }> = {
  likely_pending: { label: "likely pending", tone: "info" },
  data_quality: { label: "data quality issue", tone: "warning" },
  manual_investigation: { label: "needs manual investigation", tone: "warning" },
};

/** The AI Analysis block — shown under the queue explanation when no matcher
 *  produced a candidate the reviewer can act on. It classifies the hold and
 *  never proposes a match. */
function AnalysisSection({ analysis }: { analysis: Analysis }) {
  const kind = ANALYSIS_KINDS[analysis.classification];
  return (
    <section className="relative rounded-xl border border-human/30 bg-human/10 p-5 pl-12">
      <SparklesIcon className="absolute left-4 top-5 h-4 w-4 text-human" />
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-human">
          {analysis.label}
        </p>
        {kind && <Badge tone={kind.tone}>{kind.label}</Badge>}
        {analysis.model && <Badge tone="ai">{analysis.model}</Badge>}
      </div>

      <p className="mt-2 text-sm font-light leading-relaxed text-human-ink">
        {analysis.rationale}
      </p>

      <div className="mt-3 flex items-center justify-between rounded-lg border border-human/20 bg-raised/60 px-3.5 py-2.5">
        <span className="microlabel">Analysis confidence</span>
        {analysis.confidence != null ? (
          <ConfidenceMeter value={analysis.confidence} />
        ) : (
          <span className="num text-xs text-slate-500">—</span>
        )}
      </div>

      {analysis.missing_sources.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          <span className="microlabel mr-1">Missing sources</span>
          {analysis.missing_sources.map((s) => (
            <Badge key={s} tone={SOURCE_TONES[s] ?? "neutral"}>
              {s}
            </Badge>
          ))}
        </div>
      )}

      {analysis.below_threshold_candidates.length > 0 && (
        <div className="mt-3 rounded-lg border border-dashed border-human/30 bg-raised/30 px-3 py-2">
          <p className="microlabel mb-1.5">Below threshold — reference only</p>
          <ul className="space-y-1">
            {analysis.below_threshold_candidates.map((r) => (
              <li key={r.transaction_id} className="flex items-center gap-2 text-xs">
                <span className="num text-slate-200">{r.external_ref}</span>
                <Badge tone={SOURCE_TONES[r.source] ?? "neutral"}>{r.source}</Badge>
                <span className="num ml-auto text-slate-500">
                  {formatINR(r.amount)} · {formatDate(r.txn_date)} · sim {r.similarity.toFixed(3)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

/** The recommendation section — AI verdicts read above the raw data. */
function RecommendationSection({ rec }: { rec: Recommendation }) {
  const aiShaped = rec.verdict !== null || rec.stage === "ai" || rec.stage === "semantic";

  if (!aiShaped) {
    let headline = "Why this reached the queue";
    let detail: string | null = null;
    if (rec.incomplete_reason) {
      if (rec.incomplete_reason.startsWith("missing source(s)")) {
        headline = "Incomplete group — cannot auto-confirm";
        detail =
          `The rules linked records across ${
            rec.incomplete_reason.replace("missing source(s)", "missing source(s):")
          }. Auto-resolve needs agreement across every source, so this group ` +
          "stays open for a decision.";
      } else if (rec.incomplete_reason.includes("materiality")) {
        headline = "Held on amount variance";
        detail = "The records agree on identity but differ beyond materiality limits.";
      } else if (rec.incomplete_reason.includes("no candidate")) {
        headline = "No candidate found";
        detail = "No matcher stage produced a counterparty for this transaction.";
      }
    }
    return (
      <>
        <section className="rounded-xl border border-hairline bg-surface p-4">
          <div className="flex items-center justify-between gap-2">
            <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-slate-400">
              {headline}
            </p>
            {rec.stage && <Badge tone={matchTypeTone(rec.stage)}>{rec.stage}</Badge>}
          </div>
          {detail && (
            <p className="mt-2 text-sm font-medium leading-relaxed text-slate-200">{detail}</p>
          )}
          {rec.confidence_score && (
            <div className="mt-3 flex items-center justify-between rounded-lg border border-hairline bg-raised/50 px-3.5 py-2.5">
              <span className="microlabel">Stage confidence</span>
              <ConfidenceMeter value={rec.confidence_score} />
            </div>
          )}
          {rec.rationale && (
            <blockquote className="mt-3 border-l-2 border-slate-700 pl-3 text-xs italic leading-relaxed text-slate-400">
              {rec.rationale}
            </blockquote>
          )}
        </section>
        {rec.analysis && <AnalysisSection analysis={rec.analysis} />}
      </>
    );
  }

  const verdict = rec.verdict ? VERDICTS[rec.verdict] : null;
  let summary: string | null = null;
  if (rec.verdict === "needs_human") {
    if (
      rec.floor_met === false &&
      rec.similarity != null &&
      rec.similarity_autoresolve_min != null
    ) {
      summary =
        `The AI matched these records, but similarity (${rec.similarity.toFixed(3)}) sits below the ` +
        `auto-resolve floor (${rec.similarity_autoresolve_min.toFixed(3)}) — the joint-evidence gate ` +
        `sent it to you.`;
    } else if (rec.blocked_reason === "materiality") {
      summary =
        "The AI believes these records match, but the amount differs beyond materiality limits — " +
        "the gate held it for human review.";
    } else {
      summary = "The AI could not decide confidently, so this item was kept for human review.";
    }
  } else if (rec.verdict === "match") {
    summary = "The AI is confident these records belong together — confirm to resolve.";
  } else if (rec.verdict === "no_match") {
    summary = "No counterparty satisfied the match criteria.";
  }

  const floorLine =
    rec.similarity != null && rec.similarity_autoresolve_min != null
      ? rec.floor_met
        ? `similarity ${rec.similarity.toFixed(3)} — cleared the ${rec.similarity_autoresolve_min.toFixed(3)} auto-resolve floor`
        : `similarity ${rec.similarity.toFixed(3)} — below the ${rec.similarity_autoresolve_min.toFixed(3)} auto-resolve floor`
      : rec.similarity != null
        ? `similarity ${rec.similarity.toFixed(3)}`
        : null;
  const floorOk = rec.floor_met !== false;

  return (
    <section className="relative overflow-hidden rounded-xl border border-human/30 bg-human/10 p-5 pl-12">
      <SparklesIcon className="absolute left-4 top-5 h-4 w-4 text-human" />
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-human">
          AI recommendation
        </p>
        {verdict && <Badge tone={verdict.tone}>{verdict.label}</Badge>}
        {rec.stage && <Badge tone="ai">{rec.stage}</Badge>}
      </div>

      {summary && (
        <p className="mt-2 text-sm font-light leading-relaxed text-human-ink">{summary}</p>
      )}

      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        <div className="flex items-center justify-between rounded-lg border border-human/20 bg-raised/60 px-3.5 py-2.5">
          <span className="microlabel">Confidence</span>
          {rec.confidence_score ? (
            <ConfidenceMeter value={rec.confidence_score} />
          ) : (
            <span className="num text-xs text-slate-500">—</span>
          )}
        </div>
        {floorLine && (
          <div
            className="flex items-center justify-between gap-2 rounded-lg border border-human/20 bg-raised/60 px-3.5 py-2.5"
            title="semantic similarity vs. matching.ai.similarity_autoresolve_min"
          >
            <span className="microlabel">Similarity</span>
            <span
              className={cn(
                "num text-right text-xs leading-tight",
                floorOk ? "text-success" : "text-critical",
              )}
            >
              {floorLine}
            </span>
          </div>
        )}
      </div>

      <blockquote className="mt-3 border-l-2 border-human/50 pl-3 text-sm font-light italic leading-relaxed text-human-ink">
        {rec.rationale ?? "No rationale recorded."}
      </blockquote>
    </section>
  );
}

export function ReviewDrawer({
  target,
  onClose,
  onActionCompleted,
}: {
  target: DrawerState;
  onClose: () => void;
  onActionCompleted: () => void;
}) {
  const [exception, setException] = useState<ExceptionDetail | null>(null);
  const [match, setMatch] = useState<MatchDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [candidateQuery, setCandidateQuery] = useState("");
  const [flash, setFlash] = useState<{ ok: boolean; message: string } | null>(null);
  const [analysisPending, setAnalysisPending] = useState(false);
  const candidatesRef = useRef<HTMLElement | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      if (target.kind === "exception") {
        setMatch(null);
        const detail = await api.exceptionDetail(target.id);
        setException(detail);
        setAnalysisPending(detail.analysis_status === "pending");
      } else {
        setException(null);
        setAnalysisPending(false);
        setMatch(await api.matchDetail(target.id));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load detail");
    } finally {
      setLoading(false);
    }
  }, [target]);

  useEffect(() => {
    load();
  }, [load]);

  // When an exception opens while its AI analysis is still being generated off
  // the request path, poll the analysis endpoint until it lands, then attach it
  // to the recommendation block in place (no need to reload the whole drawer).
  useEffect(() => {
    if (target.kind !== "exception" || !analysisPending) return;
    let cancelled = false;
    let attempts = 0;
    const id = window.setInterval(async () => {
      attempts += 1;
      if (cancelled || attempts > 40) {
        window.clearInterval(id);
        setAnalysisPending(false);
        return;
      }
      try {
        const analysis = await api.exceptionAnalysis(target.id);
        if (cancelled) return;
        if (analysis) {
          window.clearInterval(id);
          setAnalysisPending(false);
          setException((prev) => {
            if (!prev?.recommendation) return prev;
            return { ...prev, recommendation: { ...prev.recommendation, analysis } };
          });
        }
      } catch {
        // transient poll failure — keep trying until the attempt cap
      }
    }, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [target, analysisPending]);

  async function act(action: () => Promise<{ message: string }>) {
    setBusy(true);
    setFlash(null);
    try {
      const result = await action();
      setFlash({ ok: true, message: result.message });
      onActionCompleted();
      await load();
    } catch (err) {
      setFlash({
        ok: false,
        message: err instanceof Error ? err.message : "action failed",
      });
    } finally {
      setBusy(false);
    }
  }

  // The recommendation arrives prebuilt from the API for both entry points.
  const rec = match?.recommendation ?? exception?.recommendation ?? null;
  const matchTypeLabel =
    match?.match_type ?? exception?.related_matches.find((m) => m.status === "proposed")?.match_type ?? null;

  // The proposal the reviewer acts on: the linked proposal for exceptions,
  // the loaded match when the drawer opened from a proposal row.
  const primaryProposal =
    target.kind === "exception"
      ? (exception?.related_matches.find((m) => m.status === "proposed") ??
        exception?.related_matches[0] ??
        null)
      : match;

  const proposalActionable = primaryProposal != null && primaryProposal.status === "proposed";
  const exceptionOpen =
    exception != null && ["open", "in_review", "escalated"].includes(exception.status);
  const manualAvailable = exceptionOpen && exception!.candidates.length > 0;
  const notesAvailable = proposalActionable || exceptionOpen;

  function manualMatchTo(candidate: Candidate) {
    if (!exception?.transaction) return;
    const replaceId = primaryProposal?.status === "proposed" ? primaryProposal.id : undefined;
    return act(() =>
      api.manualMatch(
        [exception.transaction!.id, candidate.transaction_id],
        note.trim() || `manual match to ${candidate.external_ref}`,
        replaceId,
      ),
    );
  }

  function startManualMatch() {
    setCandidateQuery("");
    candidatesRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const shownCandidates = useMemo(() => {
    const q = candidateQuery.trim().toLowerCase();
    const all = exception?.candidates ?? [];
    if (!q) return all;
    return all.filter((c) =>
      [c.external_ref, c.source, c.narration ?? ""].some((s) => s.toLowerCase().includes(q)),
    );
  }, [exception, candidateQuery]);

  const notePlaceholder = proposalActionable
    ? "e.g. confirmed via bank portal — batched payout for Aug 21"
    : "e.g. legitimate bank charge that got flagged";

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
                  <Badge tone={target.kind === "exception" ? "danger" : "ai"}>
                    {target.kind}
                  </Badge>
                  {exception && (
                    <Badge tone={priorityTone(exception.priority)}>{exception.priority}</Badge>
                  )}
                  {matchTypeLabel && (
                    <Badge tone={matchTypeTone(matchTypeLabel)}>{matchTypeLabel}</Badge>
                  )}
                  {(match?.status || exception?.status) && (
                    <Badge tone={statusTone(match?.status ?? exception!.status)}>
                      {(match?.status ?? exception!.status).replace(/_/g, " ")}
                    </Badge>
                  )}
                </div>
                <h2 className="mt-1.5 truncate text-[17px] font-semibold capitalize tracking-tight text-foreground">
                  {exception ? exception.exception_type.replace(/_/g, " ") : `${matchTypeLabel ?? ""} proposal`}
                </h2>
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
          {flash && (
            <div
              className={cn(
                "rounded-lg border px-3 py-2 text-xs",
                flash.ok
                  ? "border-success/40 bg-success/10 text-success"
                  : "border-critical/40 bg-critical/10 text-critical",
              )}
            >
              {flash.message}
            </div>
          )}

          {loading && (
            <div className="space-y-4">
              <Skeleton className="h-24 w-full" />
              <div className="grid grid-cols-2 gap-3">
                <Skeleton className="h-44" />
                <Skeleton className="h-44" />
              </div>
              <Skeleton className="h-10 w-full" />
            </div>
          )}

          {!loading && error && (
            <div className="rounded-lg border border-critical/40 bg-critical/10 px-3 py-2.5 text-xs text-critical">
              {error}
            </div>
          )}

          {/* AI Recommendation — the first thing the reviewer reads. */}
          {!loading && rec && <RecommendationSection rec={rec} />}

          {/* AI Analysis is still being generated off the request path. */}
          {!loading && analysisPending && exception?.recommendation && (
            <section className="relative rounded-xl border border-human/30 bg-human/10 p-5 pl-12">
              <SparklesIcon className="absolute left-4 top-5 h-4 w-4 text-human animate-pulse" />
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-[11px] font-semibold uppercase tracking-[0.1em] text-human">
                  AI Analysis
                </p>
                <Badge tone="info">generating…</Badge>
              </div>
              <p className="mt-2 text-sm font-light leading-relaxed text-human-ink">
                The reasoning agent is classifying this hold. It will appear here
                automatically.
              </p>
              <div className="mt-3 flex items-center gap-2 text-xs text-slate-500">
                <span className="inline-block h-5 w-24 animate-pulse rounded bg-raised" />
                <span className="inline-block h-5 w-40 animate-pulse rounded bg-raised" />
              </div>
            </section>
          )}

          {/* Prior decision note, shown once the item is closed. */}
          {!loading && exception?.resolution_note && exception.status !== "open" && exception.status !== "in_review" && (
            <section className="rounded-lg border border-info/40 bg-info/10 px-3 py-2.5">
              <p className="microlabel text-info">Resolution note</p>
              <p className="mt-1 text-xs italic leading-relaxed text-info-soft">
                “{exception.resolution_note}”
              </p>
            </section>
          )}

          {/* Side-by-side records */}
          {!loading && match && match.participants.length > 0 && (
            <section>
              <SectionLabel>Matched records</SectionLabel>
              <div className="grid gap-3 sm:grid-cols-2">
                {match.participants.map((p) => (
                  <RecordCard key={p.id} txn={p} />
                ))}
              </div>
            </section>
          )}

          {!loading && exception?.transaction && (
            <section>
              <SectionLabel>Record under review</SectionLabel>
              <RecordCard txn={exception.transaction} highlight />
              <p className="num mt-2 flex items-center gap-3 text-xs text-slate-500">
                <span>
                  impact{" "}
                  <span className="font-medium text-slate-300">
                    {formatINR(exception.amount_impact ?? "0")}
                  </span>
                </span>
                <span className="text-slate-700">·</span>
                <span>opened {formatDate(exception.opened_at)}</span>
              </p>
            </section>
          )}

          {/* Original settlement a refund reverses — read-only reference, not a match */}
          {!loading && exception?.original_transaction && (
            <section>
              <SectionLabel>
                Original settlement this refund reverses — read-only reference
              </SectionLabel>
              <RecordCard txn={exception.original_transaction} />
              <p className="mt-2 rounded-md border border-info/20 bg-info/5 px-2.5 py-1.5 text-xs text-info-soft">
                Shared {exception.transaction?.source === "erp" ? "payment_ref" : "payment_id"} — shown for
                context only. Resolving it does not create or change any match.
              </p>
            </section>
          )}

          {/* Linked proposals */}
          {!loading && exception && exception.related_matches.length > 0 && (
            <section>
              <SectionLabel>Linked proposals for this record</SectionLabel>
              <ul className="space-y-2">
                {exception.related_matches.map((m) => (
                  <li
                    key={m.id}
                    className={cn(
                      "flex items-center gap-3 rounded-lg border bg-raised/40 px-3 py-2.5",
                      m.status === "proposed" ? "border-warning/40" : "border-hairline",
                    )}
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <Badge tone={matchTypeTone(m.match_type)}>{m.match_type}</Badge>
                        <Badge tone={statusTone(m.status)}>{m.status}</Badge>
                      </div>
                      {m.rationale && (
                        <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-slate-500">
                          {m.rationale}
                        </p>
                      )}
                    </div>
                    <ConfidenceMeter value={m.confidence_score} className="shrink-0" />
                  </li>
                ))}
              </ul>
            </section>
          )}

          {/* Ranked candidates for unmatched records — the manual-match picker */}
          {!loading &&
            exception &&
            exception.candidates.length > 0 &&
            ["open", "in_review"].includes(exception.status) && (
              <section ref={candidatesRef}>
                <div className="mb-2.5 flex items-center justify-between gap-2">
                  <SectionLabel>Possible counterparts — ranked</SectionLabel>
                  <div className="relative">
                    <SearchIcon className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-600" />
                    <input
                      value={candidateQuery}
                      onChange={(e) => setCandidateQuery(e.target.value)}
                      placeholder="search ref, source, narration"
                      className={cn(FIELD_CLASS, "w-56 py-2 pl-8 text-[13px]")}
                    />
                  </div>
                </div>
                {shownCandidates.length === 0 ? (
                  <p className="rounded-lg border border-hairline bg-raised/30 px-3.5 py-3 text-xs text-slate-500">
                    No candidates match “{candidateQuery}”.
                  </p>
                ) : (
                  <ul className="divide-y divide-slate-800/60 overflow-hidden rounded-lg border border-hairline">
                    {shownCandidates.map((c) => (
                      <li
                        key={c.transaction_id}
                        className="group flex items-center gap-3 bg-raised/30 px-3 py-3 transition-colors hover:bg-gloss"
                      >
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2">
                            <span className="num truncate text-[13px] text-slate-200">
                              {c.external_ref}
                            </span>
                            <Badge tone={SOURCE_TONES[c.source] ?? "neutral"}>{c.source}</Badge>
                          </div>
                          <p className="num mt-0.5 text-xs text-slate-500">
                            {formatINR(c.amount)} · {formatDate(c.txn_date)}
                          </p>
                        </div>
                        <span
                          className={cn(
                            "num text-xs font-medium",
                            c.score >= 0.75
                              ? "text-success"
                              : c.score >= 0.5
                                ? "text-warning"
                                : "text-slate-500",
                          )}
                          title="composite similarity score"
                        >
                          {c.score.toFixed(2)}
                        </span>
                        <Button
                          disabled={busy}
                          onClick={() => manualMatchTo(c)}
                          variant="secondary"
                          className="shrink-0 px-3 py-1.5 text-xs"
                        >
                          Match
                          <ArrowRightIcon className="h-3 w-3" />
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            )}
        </div>

        {/* ── Action bar: exactly four options ──────────────── */}
        {!loading && !error && (
          <footer className="border-t border-hairline bg-surface/90 px-6 py-4 backdrop-blur">
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={busy || !proposalActionable}
                onClick={() => primaryProposal && act(() => api.approveMatch(primaryProposal.id, note))}
                className="min-w-32 flex-1"
                title={proposalActionable ? "Confirm the proposed match" : "No open proposal to approve"}
              >
                <CheckIcon className="h-3.5 w-3.5" />
                Approve
              </Button>
              <Button
                disabled={busy || !proposalActionable}
                onClick={() => primaryProposal && act(() => api.rejectMatch(primaryProposal.id, note))}
                variant="danger"
                className="min-w-24"
                title={proposalActionable ? "The proposed match is wrong" : "No open proposal to reject"}
              >
                <XIcon className="h-3.5 w-3.5" />
                Reject
              </Button>
              {exception && (
                <Button
                  disabled={busy || !manualAvailable}
                  onClick={startManualMatch}
                  variant="secondary"
                  className="min-w-24 flex-1"
                  title={
                    manualAvailable
                      ? "Pick a different counterpart for this record"
                      : "No candidates to match against"
                  }
                >
                  <ArrowRightIcon className="h-3.5 w-3.5" />
                  Manually match
                </Button>
              )}
              <Button
                disabled={busy || !exceptionOpen}
                onClick={() => exception && act(() => api.dismissException(exception.id, note))}
                variant="ghost"
                className="min-w-20 border border-transparent text-slate-400 hover:border-hairline hover:text-slate-200"
                title={exceptionOpen ? "Not an exception — close without a match" : "Closed"}
              >
                Dismiss
              </Button>

              {!exceptionOpen && !proposalActionable && (
                <p className="w-full py-1 text-center text-xs text-slate-600">
                  This item is closed — no further actions available.
                </p>
              )}

              {notesAvailable && (
                <label className="mt-3 block w-full space-y-1.5">
                  <span className="microlabel">
                    Note — optional, visible with the decision afterward
                  </span>
                  <textarea
                    value={note}
                    onChange={(e) => setNote(e.target.value)}
                    placeholder={notePlaceholder}
                    rows={2}
                    className={cn(FIELD_CLASS, "resize-none")}
                  />
                </label>
              )}
            </div>
          </footer>
        )}
      </aside>
    </div>
  );
}

export type { DrawerState };