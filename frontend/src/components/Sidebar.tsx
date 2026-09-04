import type { ReactNode } from "react";

import {
  CheckCircleIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  FileTextIcon,
  LayersIcon,
  LayoutGridIcon,
  ListChecksIcon,
  SlidersIcon,
  UploadCloudIcon,
} from "@/components/ui/icons";
import { cn } from "@/lib/utils";

export type SectionKey = "ingest" | "review" | "resolved" | "dashboard" | "policy" | "reports";

export interface NavSection {
  key: SectionKey;
  label: string;
  icon: ReactNode;
  keywords: string[];
}

export const NAV_SECTIONS: NavSection[] = [
  {
    key: "dashboard",
    label: "Dashboard",
    icon: <LayoutGridIcon className="h-4 w-4" />,
    keywords: ["overview", "charts", "trends"],
  },
  {
    key: "resolved",
    label: "Auto-Resolved",
    icon: <CheckCircleIcon className="h-4 w-4" />,
    keywords: ["auto", "confirmed", "stage", "deterministic", "fuzzy", "semantic", "ai"],
  },
  {
    key: "review",
    label: "Human Review",
    icon: <ListChecksIcon className="h-4 w-4" />,
    keywords: ["exceptions", "proposals", "approve", "reject", "dismiss", "reviewed"],
  },
  {
    key: "policy",
    label: "Policy",
    icon: <SlidersIcon className="h-4 w-4" />,
    keywords: ["thresholds", "config", "gates"],
  },
  {
    key: "reports",
    label: "Reports",
    icon: <FileTextIcon className="h-4 w-4" />,
    keywords: ["export", "csv", "pdf", "webhook", "erp"],
  },
  {
    key: "ingest",
    label: "Data sources",
    icon: <UploadCloudIcon className="h-4 w-4" />,
    keywords: ["upload", "transactions", "import", "bank", "history", "batch"],
  },
];

export const SECTION_ICON: Record<SectionKey, ReactNode> = Object.fromEntries(
  NAV_SECTIONS.map((s) => [s.key, s.icon]),
) as Record<SectionKey, ReactNode>;

const SIDEBAR_EXPANDED = 248;
const SIDEBAR_RAIL = 76;

interface SidebarProps {
  active: SectionKey;
  onNavigate: (section: SectionKey) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
}

export function Sidebar({
  active,
  onNavigate,
  collapsed,
  onToggleCollapsed,
  mobileOpen,
  onCloseMobile,
}: SidebarProps) {
  return (
    <>
      {/* Desktop: sticky in-flow spacer pins a fixed-width rail next to main. */}
      <div
        className="sticky top-0 hidden h-screen shrink-0 lg:block"
        style={{ flexBasis: collapsed ? SIDEBAR_RAIL : SIDEBAR_EXPANDED, transition: "flex-basis 220ms ease" }}
      >
        <aside
          className="fixed inset-y-0 left-0 flex flex-col border-r border-hairline bg-canvas"
          style={{ width: collapsed ? SIDEBAR_RAIL : SIDEBAR_EXPANDED, transition: "width 220ms ease" }}
        >
          <SidebarInner
            active={active}
            onNavigate={onNavigate}
            collapsed={collapsed}
            onToggleCollapsed={onToggleCollapsed}
          />
        </aside>
      </div>

      {/* Mobile: off-canvas overlay drawer. */}
      <div className={cn("fixed inset-0 z-40 lg:hidden", mobileOpen ? "" : "pointer-events-none")}>
        <button
          onClick={onCloseMobile}
          aria-label="Close navigation"
          tabIndex={mobileOpen ? 0 : -1}
          className={cn(
            "absolute inset-0 block h-full w-full bg-slate-950/60 backdrop-blur-[1px] transition-opacity",
            mobileOpen ? "opacity-100" : "opacity-0",
          )}
        />
        <aside
          className={cn(
            "absolute inset-y-0 left-0 flex w-64 flex-col border-r border-hairline bg-canvas shadow-2xl transition-transform duration-200",
            mobileOpen ? "translate-x-0" : "-translate-x-full",
          )}
        >
          <SidebarInner
            active={active}
            onNavigate={onNavigate}
            collapsed={false}
            onToggleCollapsed={onToggleCollapsed}
            mobile
            onCloseMobile={onCloseMobile}
          />
        </aside>
      </div>
    </>
  );
}

interface SidebarInnerProps {
  active: SectionKey;
  onNavigate: (section: SectionKey) => void;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  mobile?: boolean;
  onCloseMobile?: () => void;
}

function SidebarInner({
  active,
  onNavigate,
  collapsed,
  onToggleCollapsed,
  mobile = false,
  onCloseMobile,
}: SidebarInnerProps) {
  const showLabels = mobile || !collapsed;

  return (
    <>
      <div className={cn("flex items-center gap-3 border-b border-hairline", showLabels ? "px-4 py-5" : "px-0 py-5 justify-center")}>
        <div className="animate-logo-breathe flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br from-green-500 to-green-700">
          <LayersIcon className="h-4.5 w-4.5 text-white" />
        </div>
        {showLabels && (
          <div className="min-w-0">
            <h1 className="truncate font-display text-[15px] font-semibold leading-tight tracking-tight text-foreground">
              RECONX
            </h1>
            <p className="truncate text-[11px] leading-tight text-slate-500">
              AI Finance Controller
            </p>
          </div>
        )}
      </div>

      <nav className="flex-1 space-y-1.5 overflow-y-auto overflow-x-hidden px-2.5 py-4" aria-label="Primary navigation">
        {NAV_SECTIONS.map((section) => {
          const isActive = section.key === active;
          return (
            <button
              key={section.key}
              onClick={() => {
                onNavigate(section.key);
                if (mobile) onCloseMobile?.();
              }}
              title={!showLabels ? section.label : undefined}
              className={cn(
                "group flex w-full items-center gap-3 rounded-lg text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-green-500/60",
                showLabels ? "px-3 py-2.5" : "justify-center px-0 py-3",
                isActive
                  ? "bg-green-600 text-white shadow-[inset_0_1px_0_rgba(255,255,255,0.16),0_1px_3px_rgba(0,0,0,0.35)]"
                  : "text-slate-400 hover:bg-gloss hover:text-slate-200",
              )}
            >
              <span
                className={cn(
                  "shrink-0 transition-colors",
                  isActive ? "text-white" : "text-slate-500 group-hover:text-slate-300",
                )}
              >
                {section.icon}
              </span>
              {showLabels && <span className="truncate">{section.label}</span>}
            </button>
          );
        })}
      </nav>

      <div className="border-t border-hairline px-2.5 py-3">
        {!showLabels ? (
          <button
            onClick={onToggleCollapsed}
            title="Expand sidebar"
            className="mx-auto flex h-8 w-8 items-center justify-center rounded-lg text-slate-500 transition-colors hover:bg-gloss hover:text-slate-300"
          >
            <ChevronRightIcon className="h-4 w-4" />
          </button>
        ) : (
          <button
            onClick={onToggleCollapsed}
            className="flex w-full items-center justify-center gap-2 rounded-lg px-2.5 py-2 text-xs font-medium text-slate-500 transition-colors hover:bg-gloss hover:text-slate-300"
          >
            <ChevronLeftIcon className="h-3.5 w-3.5" />
            {!mobile && <span>Collapse</span>}
          </button>
        )}
      </div>
    </>
  );
}