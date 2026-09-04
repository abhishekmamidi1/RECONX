import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";

/**
 * Confidence is the product's core currency — it gets a real visualization,
 * not a bare number. Bands mirror the policy gate, using the semantic
 * tokens: success (green) ≥0.90 auto-resolve eligible, warning (amber)
 * ≥0.60 plausible, critical (rose) below.
 */
export function confidenceBand(value: number): "high" | "mid" | "low" {
  if (value >= 0.9) return "high";
  if (value >= 0.6) return "mid";
  return "low";
}

const BAND_FILL = {
  high: "bg-success",
  mid: "bg-warning",
  low: "bg-critical",
} as const;

const BAND_TEXT = {
  high: "text-success",
  mid: "text-warning",
  low: "text-critical",
} as const;

export function ConfidenceMeter({
  value,
  className,
}: {
  /** string from API ("0.5500") or number 0..1 */
  value: string | number;
  className?: string;
}) {
  const numeric =
    typeof value === "number" ? Math.max(0, Math.min(1, value)) : parseFloat(value);
  const safe = Number.isFinite(numeric) ? numeric : 0;
  const band = confidenceBand(safe);

  // Sweep the fill in on mount so confidence reads as a living measure.
  // Reduced-motion users get the final value instantly via the global rules.
  const [filled, setFilled] = useState(safe >= 1 ? 100 : 0);
  useEffect(() => {
    const raf = requestAnimationFrame(() => setFilled(Math.round(safe * 100)));
    return () => cancelAnimationFrame(raf);
  }, [safe]);

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-800/70">
        <div
          className={cn(
            "h-full rounded-full transition-[width] duration-700 ease-out",
            BAND_FILL[band],
          )}
          style={{ width: `${filled}%` }}
        />
      </div>
      <span className={cn("num text-xs font-medium", BAND_TEXT[band])}>
        {safe.toFixed(2)}
      </span>
    </div>
  );
}
