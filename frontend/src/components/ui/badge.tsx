import { cn } from "@/lib/utils";

/**
 * Semantic tone system, driven by the --color-* tokens in index.css
 * (success/warning/critical/info + source identity hues).
 *
 * success  — resolved / confirmed / money in (green)
 * warning  — awaiting human review, near-threshold confidence (amber)
 * danger   — critical materiality, rejected, debits (rose)
 * info     — in-progress, informational (sky)
 * ai       — anything produced by the reasoning layer (violet = "machine thought")
 * source tones — razorpay / bank / erp identity colors from the palette tokens
 */
export type Tone =
  | "neutral"
  | "success"
  | "warning"
  | "danger"
  | "info"
  | "ai"
  | "razorpay"
  | "bank"
  | "erp";

const TONES: Record<Tone, string> = {
  neutral: "border-slate-700/80 bg-slate-800/50 text-slate-300",
  success: "border-success/40 bg-success/10 text-success",
  warning: "border-warning/40 bg-warning/10 text-warning",
  danger: "border-critical/40 bg-critical/10 text-critical",
  info: "border-info/40 bg-info/10 text-info",
  ai: "border-human/40 bg-human/10 text-human",
  razorpay: "border-source-razorpay/40 bg-source-razorpay/10 text-source-razorpay",
  bank: "border-source-bank/40 bg-source-bank/10 text-source-bank",
  erp: "border-source-erp/40 bg-source-erp/10 text-source-erp",
};

export const SOURCE_TONES: Record<string, Tone> = {
  razorpay: "razorpay",
  bank: "bank",
  erp: "erp",
};

export function Badge({
  tone = "neutral",
  className,
  children,
}: {
  tone?: Tone;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-semibold uppercase leading-[17px] tracking-[0.06em]",
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** Priority → tone mapping used by queue rows and the drawer. */
export function priorityTone(priority: string): Tone {
  switch (priority) {
    case "critical":
      return "danger";
    case "high":
      return "warning";
    case "medium":
      return "neutral";
    default:
      return "neutral";
  }
}

/** Status → tone mapping for exception/match workflow states. */
export function statusTone(status: string): Tone {
  switch (status) {
    case "confirmed":
    case "resolved":
      return "success";
    case "proposed":
    case "in_review":
      return "warning";
    case "escalated":
    case "rejected":
      return "danger";
    default:
      return "neutral";
  }
}

/** Match type → tone; AI-produced matches get the violet treatment. */
export function matchTypeTone(matchType: string | null): Tone {
  if (
    matchType === "ai" ||
    matchType === "semantic" ||
    matchType === "batch"
  )
    return "ai";
  if (matchType === "manual") return "info";
  return "neutral";
}
