import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { api, type PolicyField } from "@/api/client";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  AlertIcon,
  CheckCircleIcon,
  FileTextIcon,
  InfoIcon,
  LayersIcon,
  SlidersIcon,
  SparklesIcon,
} from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import { cn, formatDateTime, formatINR } from "@/lib/utils";

const GROUP_ORDER = [
  "Matching thresholds",
  "Auto-resolve gates",
  "Materiality rules",
  "Batch grouping",
  "Other",
];

const GROUP_META: Record<string, { icon: ReactNode; description: string }> = {
  "Matching thresholds": {
    icon: <SlidersIcon className="h-4 w-4" />,
    description: "How aggressively exact, fuzzy, and semantic matchers propose candidates on the next run.",
  },
  "Auto-resolve gates": {
    icon: <SparklesIcon className="h-4 w-4" />,
    description: "Confidence and joint-evidence floors the pipeline must clear before it settles on its own.",
  },
  "Materiality rules": {
    icon: <AlertIcon className="h-4 w-4" />,
    description: "When a gap is too large to auto-resolve regardless of confidence.",
  },
  "Batch grouping": {
    icon: <LayersIcon className="h-4 w-4" />,
    description: "The deferred many-to-one aggregator for batched payout components.",
  },
  "Other": {
    icon: <FileTextIcon className="h-4 w-4" />,
    description: "Registered keys without a dedicated review surface.",
  },
};

const NUMBER_CLASS =
  "num w-32 rounded-md border border-hairline bg-raised px-2.5 py-2 text-right text-sm text-slate-200 transition-colors focus:border-green-500/70 focus:outline-none focus:ring-2 focus:ring-green-500/20";

const UNIT_LABEL: Record<string, string> = {
  days: "days",
  inr: "₹",
  count: "items",
  "score (0-100)": "/100",
  "score (0-1)": "0-1",
  "fraction (0-1)": "0-1",
  "pct (0-0.1)": "%",
  "pct (0-0.5)": "%",
};

interface SliderSpec {
  min: number;
  max: number;
  step: number;
  kind: "ratio" | "pct";
}

const SLIDER_KEYS: Record<string, SliderSpec> = {
  "matching.semantic.similarity_threshold": { min: 0, max: 1, step: 0.01, kind: "ratio" },
  "gate.ai_min_confidence_autoresolve": { min: 0, max: 1, step: 0.01, kind: "ratio" },
  "matching.ai.similarity_autoresolve_min": { min: 0, max: 1, step: 0.01, kind: "ratio" },
  "materiality.max_discrepancy_pct": { min: 0, max: 1, step: 0.01, kind: "ratio" },
  "matching.fuzzy.amount_tolerance_pct": { min: 0, max: 0.1, step: 0.001, kind: "pct" },
  "matching.batch.amount_tolerance_pct": { min: 0, max: 0.5, step: 0.001, kind: "pct" },
};

interface FieldState {
  draft: string;
  error: string | null;
  saving: boolean;
  flash: { ok: boolean; text: string } | null;
}

function initialState(field: PolicyField): FieldState {
  const value = field.value;
  return {
    draft:
      typeof value === "boolean"
        ? String(value)
        : value === null || value === undefined
          ? ""
          : String(value),
    error: null,
    saving: false,
    flash: null,
  };
}

function toPayload(field: PolicyField, raw: string): unknown {
  if (field.value_type === "bool") return raw === "true";
  if (field.value_type === "int") return parseInt(raw, 10);
  return parseFloat(raw);
}

function isDirty(field: PolicyField, state: FieldState): boolean {
  return (
    String(field.value) !== state.draft &&
    !(field.value_type === "bool" && String(field.value).toLowerCase() === state.draft.toLowerCase())
  );
}

function trimmed(value: number, digits: number): string {
  return parseFloat(value.toFixed(digits)).toString();
}

function formatSlider(value: number, spec: SliderSpec): { decimal: string; pct: string } {
  if (spec.kind === "pct") {
    return { decimal: trimmed(value, 3), pct: `${trimmed(value * 100, 1)}%` };
  }
  return { decimal: trimmed(value, 2), pct: `${trimmed(value * 100, 1)}%` };
}

function captionFor(field: PolicyField, draft: string): string | null {
  const raw = draft.trim();
  const v = raw === "" || Number.isNaN(Number(raw)) ? field.value : Number(raw);
  if (typeof v !== "number" || Number.isNaN(v)) return null;

  switch (field.key) {
    case "matching.deterministic.date_window_days":
      return `exact matches accepted within ±${v} day${v === 1 ? "" : "s"}`;
    case "matching.fuzzy.date_window_days":
    case "matching.batch.date_window_days":
      return `candidates considered within ±${v} day${v === 1 ? "" : "s"} of the target date`;
    case "matching.fuzzy.score_threshold":
      return `a candidate is proposed at a score of ≥ ${v}/100`;
    case "matching.semantic.top_k":
      return `retrieves the ${v} nearest neighbours per unmatched record`;
    case "matching.semantic.similarity_threshold":
      return `candidates surface at ≥ ${trimmed(v * 100, 1)}% cosine similarity`;
    case "matching.ai.similarity_autoresolve_min":
      return `below ${trimmed(v * 100, 1)}% similarity a declared match falls to human review`;
    case "gate.ai_min_confidence_autoresolve":
      return `${trimmed(v * 100, 1)}% AI confidence is required to auto-resolve`;
    case "review.force_human_above_inr":
      return `amounts ≥ ${formatINR(v)} always route to human review`;
    case "materiality.max_abs_discrepancy_inr":
      return `gaps beyond ${formatINR(v)} block auto-resolve`;
    case "materiality.max_discrepancy_pct":
      return `gaps beyond ${trimmed(v * 100, 1)}% of transaction value block auto-resolve`;
    case "matching.fuzzy.amount_tolerance_pct":
      return `amounts may differ by up to ${trimmed(v * 100, 1)}% (0.001 = 0.1%)`;
    case "matching.batch.amount_tolerance_pct":
      return `summed components must land within ${trimmed(v * 100, 1)}% of the bank credit`;
    case "matching.batch.max_components":
      return `up to ${v} gateway credits may aggregate into one bank credit`;
    default:
      return null;
  }
}

function HistoryPanel({ fieldKey }: { fieldKey: string }) {
  const [entries, setEntries] = useState<Awaited<ReturnType<typeof api.policyHistory>> | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .policyHistory(fieldKey)
      .then(setEntries)
      .catch((err) => setError(err instanceof Error ? err.message : "failed to load history"));
  }, [fieldKey]);

  if (error) return <p className="px-1 py-1.5 text-xs text-critical">{error}</p>;
  if (!entries) return <Skeleton className="mx-1 my-2 h-10 w-full" />;
  if (entries.length === 0)
    return (
      <p className="border-l-2 border-slate-800 px-3 py-1.5 text-xs italic text-slate-600">
        No recorded changes since audit began.
      </p>
    );
  return (
    <ul className="space-y-1 border-l-2 border-slate-800 py-1 pl-3">
      {entries.map((entry) => (
        <li key={entry.id} className="flex items-baseline gap-2 text-xs">
          <span className="text-slate-500">{formatDateTime(entry.created_at)}</span>
          <span className="text-slate-300">{entry.actor}</span>
          <span className="text-slate-600">changed</span>
          <span className="num text-warning/80">{JSON.stringify(entry.before_state?.value)}</span>
          <span className="text-slate-600">→</span>
          <span className="num text-success/90">{JSON.stringify(entry.after_state?.value)}</span>
        </li>
      ))}
    </ul>
  );
}

function LabelTooltip({ text }: { text: string }) {
  return (
    <span className="group/tip relative inline-flex" title={text}>
      <InfoIcon className="h-3.5 w-3.5 cursor-help text-slate-600 transition-colors group-hover/tip:text-slate-400" />
      <span className="pointer-events-none absolute bottom-full left-0 z-20 mb-1.5 w-64 rounded-lg border border-hairline bg-raised px-2.5 py-2 text-xs leading-relaxed text-slate-400 shadow-xl opacity-0 transition-opacity group-hover/tip:opacity-100">
        {text}
      </span>
    </span>
  );
}

function BoolControl({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const on = value === "true";
  const base =
    "px-3.5 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-green-500/60";
  return (
    <div className="flex overflow-hidden rounded-md border border-hairline">
      <button
        onClick={() => onChange("true")}
        className={cn(
          base,
          on ? "bg-green-600 text-white" : "bg-transparent text-slate-500 hover:text-slate-300",
        )}
      >
        Enabled
      </button>
      <button
        onClick={() => onChange("false")}
        className={cn(
          base,
          "border-l border-hairline",
          !on ? "bg-slate-800 text-slate-200" : "bg-transparent text-slate-500 hover:text-slate-300",
        )}
      >
        Disabled
      </button>
    </div>
  );
}

export function PolicyPage({ refreshSignal }: { refreshSignal: number }) {
  const [fields, setFields] = useState<PolicyField[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, FieldState>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [summary, setSummary] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.policy();
      setFields(data);
      setStates(Object.fromEntries(data.map((f) => [f.key, initialState(f)])));
      setError(null);
      setSummary(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load policy");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, refreshSignal]);

  const dirtyCount = useMemo(() => {
    if (!fields) return 0;
    return fields.filter((f) => isDirty(f, states[f.key])).length;
  }, [fields, states]);

  function updateState(key: string, patch: Partial<FieldState>) {
    setStates((prev) => ({ ...prev, [key]: { ...prev[key], ...patch } }));
  }

  async function saveOne(field: PolicyField): Promise<{ ok: boolean; changed: boolean }> {
    const state = states[field.key];
    updateState(field.key, { saving: true, error: null, flash: null });
    try {
      const payload = toPayload(field, state.draft);
      if (typeof payload === "number" && Number.isNaN(payload)) throw new Error("not a number");
      const result = await api.updatePolicy(field.key, payload);
      updateState(field.key, {
        saving: false,
        flash: result.changed
          ? { ok: true, text: `saved · was ${JSON.stringify(result.before)}` }
          : { ok: true, text: "no change" },
      });
      setFields((prev) =>
        (prev ?? []).map((f) =>
          f.key === field.key
            ? {
                ...f,
                value: result.value,
                last_changed: {
                  actor: result.actor,
                  at: new Date().toISOString(),
                  before: result.before,
                  after: result.value,
                },
              }
            : f,
        ),
      );
      return { ok: true, changed: result.changed };
    } catch (err) {
      updateState(field.key, {
        saving: false,
        error: err instanceof Error ? err.message : "save failed",
      });
      return { ok: false, changed: false };
    }
  }

  async function saveAll() {
    if (!fields || busy) return;
    const dirty = fields.filter((f) => isDirty(f, states[f.key]));
    if (dirty.length === 0) return;
    setBusy(true);
    setSummary(null);
    let saved = 0;
    let unchanged = 0;
    let failed = 0;
    for (const field of dirty) {
      const outcome = await saveOne(field);
      if (outcome.ok) {
        if (outcome.changed) saved += 1;
        else unchanged += 1;
      } else {
        failed += 1;
      }
    }
    setBusy(false);
    setSummary(
      failed > 0
        ? { ok: false, text: `${saved} saved · ${unchanged} unchanged · ${failed} rejected — fix the highlighted fields and save again` }
        : { ok: true, text: `${saved} saved · ${unchanged} unchanged` },
    );
  }

  function toggleHistory(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  if (error) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-critical/40 bg-critical/10 p-5 text-sm text-critical">
        <AlertIcon className="h-5 w-5 shrink-0" /> {error}
      </div>
    );
  }
  if (!fields) {
    return (
      <div className="space-y-5">
        {[0, 1, 2].map((g) => (
          <div key={g} className="rounded-xl border border-hairline bg-surface p-5">
            <Skeleton className="h-3 w-40" />
            <div className="mt-4 space-y-3">
              {[0, 1, 2].map((i) => (
                <Skeleton key={i} className="h-9 w-full" />
              ))}
            </div>
          </div>
        ))}
      </div>
    );
  }

  const groups = GROUP_ORDER.filter((g) => fields.some((f) => f.group === g));

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 max-w-2xl">
          <h1 className="font-display text-xl font-semibold tracking-tight text-foreground">Policy</h1>
          <p className="mt-1.5 text-[13px] leading-relaxed text-slate-500">
            Live configuration — matchers read it on the next reconciliation run. Every change is
            validated and written to the <span className="text-slate-300">audit trail</span> with
            actor, timestamp, and previous value.
          </p>
        </div>
        <Button onClick={saveAll} disabled={dirtyCount === 0 || busy} className="min-w-36">
          {busy ? "Saving…" : dirtyCount > 0 ? `Save changes (${dirtyCount})` : "Save changes"}
        </Button>
      </div>

      {summary && (
        <p
          className={cn(
            "rounded-lg border px-4 py-2.5 text-xs font-medium",
            summary.ok
              ? "border-success/40 bg-success/10 text-success"
              : "border-critical/40 bg-critical/10 text-critical",
          )}
          role="status"
        >
          {summary.text}
        </p>
      )}

      {groups.map((group) => {
        const meta = GROUP_META[group] ?? GROUP_META.Other;
        return (
          <section key={group} className="overflow-hidden rounded-xl border border-hairline bg-surface">
            <header className="flex items-start gap-3 border-b border-hairline px-5 py-5">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-gloss text-success">
                {meta.icon}
              </div>
              <div className="min-w-0">
                <h2 className="font-display text-[15px] font-semibold tracking-tight text-foreground">{group}</h2>
                <p className="mt-0.5 text-xs leading-relaxed text-slate-500">{meta.description}</p>
              </div>
            </header>
            <ul className="divide-y divide-slate-800/40">
              {fields
                .filter((f) => f.group === group)
                .map((field) => {
                  const state = states[field.key];
                  if (!state) return null;
                  const slider = SLIDER_KEYS[field.key];
                  const caption = field.value_type === "bool" ? null : captionFor(field, state.draft);

                  return (
                    <li key={field.key} className="px-5 py-5">
                      <div className="flex flex-wrap items-start justify-between gap-x-8 gap-y-3">
                        <div className="min-w-0 max-w-lg flex-1">
                          <div className="flex items-center gap-1.5">
                            <span className="text-sm font-medium text-slate-200">{field.label}</span>
                            <LabelTooltip text={field.description} />
                            {!field.editable && (
                              <Badge tone="neutral" className="ml-1">
                                not editable
                              </Badge>
                            )}
                          </div>

                          {caption && (
                            <p className="mt-1 text-xs leading-relaxed text-slate-500">{caption}</p>
                          )}

                          <div className="mt-1.5 flex items-center gap-2 text-xs text-slate-600">
                            {field.last_changed ? (
                              <>
                                last changed by{" "}
                                <span className="text-slate-400">{field.last_changed.actor}</span> ·{" "}
                                {formatDateTime(field.last_changed.at)}
                              </>
                            ) : (
                              <span>never changed since seeding</span>
                            )}
                            <button
                              onClick={() => toggleHistory(field.key)}
                              className="text-success/80 underline-offset-2 hover:text-success hover:underline"
                            >
                              {expanded.has(field.key) ? "hide history" : "history"}
                            </button>
                          </div>

                          {(state.error || state.flash) && (
                            <p
                              className={cn(
                                "mt-1.5 flex items-center gap-1.5 text-xs",
                                state.error ? "text-critical" : "text-success",
                              )}
                            >
                              {state.error ? (
                                <AlertIcon className="h-3 w-3" />
                              ) : (
                                <CheckCircleIcon className="h-3 w-3" />
                              )}
                              {state.error ?? state.flash?.text}
                            </p>
                          )}
                        </div>

                        <div className="flex shrink-0 items-center gap-3">
                          {field.editable ? (
                            slider ? (
                              (() => {
                                const parsed = parseFloat(state.draft);
                                const num = Number.isNaN(parsed)
                                  ? (field.value as number) ?? slider.min
                                  : parsed;
                                const shown = formatSlider(num, slider);
                                return (
                                  <div className="flex w-72 items-center gap-3">
                                    <input
                                      type="range"
                                      min={slider.min}
                                      max={slider.max}
                                      step={slider.step}
                                      value={num}
                                      onChange={(e) =>
                                        updateState(field.key, { draft: e.target.value })
                                      }
                                      disabled={state.saving}
                                      aria-label={field.label}
                                      className="h-1.5 w-full cursor-pointer accent-success disabled:cursor-not-allowed"
                                    />
                                    <span className="num w-24 shrink-0 text-right text-[13px] text-slate-300">
                                      {shown.decimal} <span className="text-slate-600">· {shown.pct}</span>
                                    </span>
                                  </div>
                                );
                              })()
                            ) : field.value_type === "bool" ? (
                              <BoolControl
                                value={state.draft}
                                onChange={(v) => updateState(field.key, { draft: v })}
                              />
                            ) : (
                              <div className="flex items-center gap-2">
                                <input
                                  value={state.draft}
                                  onChange={(e) => updateState(field.key, { draft: e.target.value })}
                                  onKeyDown={(e) => e.key === "Enter" && saveAll()}
                                  disabled={state.saving}
                                  inputMode={field.value_type === "int" ? "numeric" : "decimal"}
                                  className={cn(NUMBER_CLASS, state.error && "border-critical/70")}
                                />
                                {field.unit && (
                                  <span className="w-14 text-[11px] uppercase tracking-wide text-slate-600">
                                    {UNIT_LABEL[field.unit] ?? field.unit}
                                  </span>
                                )}
                              </div>
                            )
                          ) : (
                            <span className="num rounded-md border border-hairline bg-raised px-2.5 py-2 text-sm text-slate-400">
                              {String(field.value ?? "—")}
                            </span>
                          )}
                        </div>
                      </div>

                      {expanded.has(field.key) && (
                        <div className="mt-3">
                          <HistoryPanel fieldKey={field.key} />
                        </div>
                      )}
                    </li>
                  );
                })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}