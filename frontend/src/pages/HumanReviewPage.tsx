import { useState } from "react";

import { cn } from "@/lib/utils";
import { ReviewedPage } from "@/pages/ReviewedPage";
import { ReviewQueuePage } from "@/pages/ReviewQueuePage";
import { ListChecksIcon, UserIcon } from "@/components/ui/icons";
import type { QueueItemType } from "@/api/client";

type SubView = "toreview" | "reviewed";

export function HumanReviewPage({
  refreshSignal,
  onOpenItem,
}: {
  refreshSignal: number;
  onOpenItem: (kind: QueueItemType, id: string) => void;
}) {
  const [view, setView] = useState<SubView>("toreview");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1.5 rounded-lg border border-hairline bg-surface p-1">
          <TabButton
            active={view === "toreview"}
            onClick={() => setView("toreview")}
            icon={<ListChecksIcon className="h-3.5 w-3.5" />}
            label="To Be Reviewed"
          />
          <TabButton
            active={view === "reviewed"}
            onClick={() => setView("reviewed")}
            icon={<UserIcon className="h-3.5 w-3.5" />}
            label="Reviewed"
          />
        </div>
        <p className="text-xs text-slate-500">
          {view === "toreview"
            ? "Open exceptions and proposed matches awaiting a human decision."
            : "Decisions you have already made, with who, when, and the note left behind."}
        </p>
      </div>

      {view === "toreview" ? (
        <ReviewQueuePage refreshSignal={refreshSignal} onOpenItem={onOpenItem} />
      ) : (
        <ReviewedPage refreshSignal={refreshSignal} />
      )}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-md px-3.5 py-2 text-sm font-medium transition-colors",
        active
          ? "bg-gloss-strong text-foreground shadow-[inset_2px_0_0_var(--color-success)]"
          : "text-slate-500 hover:text-slate-300",
      )}
    >
      {icon}
      {label}
    </button>
  );
}
