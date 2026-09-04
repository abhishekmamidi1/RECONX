import { useId } from "react";
import {
  Area,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { AnalyticsOverview } from "@/api/client";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

const AXIS_TICK = { fill: "var(--color-muted)", fontSize: 11 };
const GRID_STROKE = "var(--color-hairline)";

/* Chart series read the semantic tokens from index.css via var() so charts
   and badges can never drift apart; axis/legend/nesting use the muted text
   token so charts flip cleanly in light mode. */
const C_SUCCESS = "var(--color-success)";
const C_WARNING = "var(--color-warning)";
const C_CRITICAL = "var(--color-critical)";
const C_INFO = "var(--color-info)";
const C_HUMAN = "var(--color-human)";

const dayLabel = (iso: string) => iso.slice(5);

const compact = (value: number) =>
  value >= 1000 ? `${(value / 1000).toFixed(value % 1000 === 0 ? 0 : 1)}k` : String(value);

/** Shared rich tooltip — dark, bordered card with per-series color dots. */
function ChartTooltip({ active, payload, label }: { active?: boolean; payload?: any[]; label?: string }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-hairline bg-canvas/95 px-3.5 py-2.5 text-xs shadow-[0_12px_32px_rgba(0,0,0,0.45)] backdrop-blur">
      <p className="mb-1.5 text-slate-500">{label}</p>
      <div className="space-y-1">
        {payload.map((entry, i) => (
          <div key={i} className="flex items-center gap-2">
            <span
              className="h-1.5 w-1.5 rounded-full"
              style={{ backgroundColor: entry.color ?? entry.stroke ?? "var(--color-muted)" }}
            />
            <span className="text-slate-400">{entry.name}</span>
            <span className="num ml-auto pl-4 font-medium text-slate-200">
              {Number(entry.value).toLocaleString("en-IN")}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function ChartCard({
  title,
  context,
  className,
  children,
}: {
  title: string;
  context?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={cn("card-interactive rounded-xl border border-hairline bg-surface p-5", className)}>
      <p className="microlabel">{title}</p>
      <div className="mt-3.5 h-64">{children}</div>
      {context && <p className="mt-2.5 text-xs text-slate-600">{context}</p>}
    </div>
  );
}

export function ResolutionFlowChart({ data }: { data: AnalyticsOverview["buckets"] }) {
  const gid = useId();
  return (
    <ResponsiveContainer width="100%" height="100%">
      <ComposedChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -22 }}>
        <defs>
          <linearGradient id={`${gid}-auto`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={C_SUCCESS} stopOpacity={0.32} />
            <stop offset="100%" stopColor={C_SUCCESS} stopOpacity={0.03} />
          </linearGradient>
          <linearGradient id={`${gid}-human`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={C_HUMAN} stopOpacity={0.34} />
            <stop offset="100%" stopColor={C_HUMAN} stopOpacity={0.03} />
          </linearGradient>
        </defs>
        <CartesianGrid stroke={GRID_STROKE} vertical={false} />
        <XAxis dataKey="date" tickFormatter={dayLabel} tick={AXIS_TICK} axisLine={{ stroke: GRID_STROKE }} tickLine={false} />
        <YAxis allowDecimals={false} tick={AXIS_TICK} axisLine={false} tickLine={false} tickFormatter={(v: number) => compact(v)} />
        <Tooltip content={<ChartTooltip />} cursor={{ stroke: "var(--color-hairline-heavy)" }} />
        <Legend wrapperStyle={{ fontSize: 11, color: "var(--color-muted)" }} iconSize={8} />
        <Area
          type="monotone"
          dataKey="auto_resolved"
          name="auto-resolved"
          stroke={C_SUCCESS}
          fill={`url(#${gid}-auto)`}
          strokeWidth={1.5}
          activeDot={{ r: 3 }}
        />
        <Area
          type="monotone"
          dataKey="human_resolved"
          name="human-resolved"
          stroke={C_HUMAN}
          fill={`url(#${gid}-human)`}
          strokeWidth={1.5}
          activeDot={{ r: 3 }}
        />
        <Line
          type="monotone"
          dataKey="rejected"
          name="rejected"
          stroke={C_CRITICAL}
          strokeWidth={1.25}
          strokeDasharray="4 3"
          dot={false}
          activeDot={{ r: 3 }}
        />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

export function ExceptionFlowChart({ data }: { data: AnalyticsOverview["buckets"] }) {
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 4, right: 4, bottom: 0, left: -22 }} barGap={2}>
        <CartesianGrid stroke={GRID_STROKE} vertical={false} />
        <XAxis dataKey="date" tickFormatter={dayLabel} tick={AXIS_TICK} axisLine={{ stroke: GRID_STROKE }} tickLine={false} />
        <YAxis allowDecimals={false} tick={AXIS_TICK} axisLine={false} tickLine={false} tickFormatter={(v: number) => compact(v)} />
        <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
        <Legend wrapperStyle={{ fontSize: 11, color: "var(--color-muted)" }} iconSize={8} />
        <Bar
          dataKey="exceptions_opened"
          name="opened"
          fill={C_WARNING}
          fillOpacity={0.75}
          radius={2}
          activeBar={{ fillOpacity: 1, radius: 4 }}
        />
        <Bar
          dataKey="exceptions_resolved"
          name="closed"
          fill={C_SUCCESS}
          fillOpacity={0.65}
          radius={2}
          activeBar={{ fillOpacity: 1, radius: 4 }}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

const DONUT_COLORS = { auto: C_SUCCESS, human: C_HUMAN };

export function SplitDonut({ split }: { split: AnalyticsOverview["resolution_split"] }) {
  const total = split.auto + split.human;
  const chartData = [
    { name: "auto-resolved", value: split.auto, key: "auto" as const },
    { name: "human-resolved", value: split.human, key: "human" as const },
  ];
  return (
    <div className="relative h-full">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <Tooltip content={<ChartTooltip />} />
          <Pie
            data={chartData}
            dataKey="value"
            nameKey="name"
            innerRadius="58%"
            outerRadius="82%"
            paddingAngle={total > 0 ? 2 : 0}
            cornerRadius={total > 0 ? 6 : 0}
            stroke="none"
          >
            {chartData.map((entry) => (
              <Cell key={entry.key} fill={DONUT_COLORS[entry.key]} fillOpacity={entry.value === 0 ? 0.12 : 0.85} />
            ))}
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
        <span className="num text-2xl font-semibold text-foreground">{total}</span>
        <span className="microlabel">decisions</span>
      </div>
    </div>
  );
}

/* Categorical matcher colors — chart-dedicated tokens plus semantic info
   (deterministic) and warning (manual). Distinct from source-identity hues. */
const MATCHER_COLORS: Record<string, string> = {
  deterministic: C_INFO,
  fuzzy: "var(--color-chart-fuzzy)",
  semantic: "var(--color-human)",
  ai: "var(--color-chart-ai)",
  batch: "var(--color-human)",
  manual: C_WARNING,
};

export function MatcherBars({ breakdown }: { breakdown: AnalyticsOverview["by_match_type"] }) {
  if (breakdown.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-slate-600">
        No matches created in this period.
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart
        data={breakdown.map((b) => ({ ...b, label: b.match_type }))}
        layout="vertical"
        margin={{ top: 0, right: 18, bottom: 0, left: 30 }}
      >
        <XAxis type="number" allowDecimals={false} hide />
        <YAxis
          type="category"
          dataKey="match_type"
          width={78}
          tick={{ ...AXIS_TICK, fontSize: 12 }}
          axisLine={false}
          tickLine={false}
        />
        <Tooltip content={<ChartTooltip />} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
        <Bar dataKey="count" radius={[0, 3, 3, 0]} barSize={14} activeBar={{ fillOpacity: 1 }}>
          {breakdown.map((entry) => (
            <Cell key={entry.match_type} fill={MATCHER_COLORS[entry.match_type] ?? "#475569"} fillOpacity={0.85} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export function ChartsSkeleton() {
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {[0, 1, 2].map((i) => (
        <div key={i} className={cn("rounded-xl border border-hairline bg-surface p-5", i === 0 && "lg:col-span-2")}>
          <Skeleton className="h-2.5 w-32" />
          <Skeleton className="mt-4 h-56 w-full" />
        </div>
      ))}
    </div>
  );
}
