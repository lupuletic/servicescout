import { type ReactNode, useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  Network,
  Boxes,
  ListChecks,
  ExternalLink,
  Gauge,
  CalendarClock,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";
import { cn } from "@/lib/cn";

const NAV = [
  { to: "/", label: "Explorer", icon: Network },
  { to: "/entities", label: "Catalog", icon: Boxes },
  { to: "/activity", label: "Activity", icon: CalendarClock },
  { to: "/operator", label: "Operator", icon: Gauge },
  { to: "/triage", label: "Triage", icon: ListChecks },
];

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem("servicescout:sidebar-collapsed") === "true";
  });

  useEffect(() => {
    window.localStorage.setItem("servicescout:sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  return (
    <div
      className={cn(
        "grid grid-rows-[56px_1fr] h-screen",
        sidebarCollapsed ? "grid-cols-[64px_minmax(0,1fr)]" : "grid-cols-[220px_minmax(0,1fr)]",
      )}
    >
      {/* Top bar */}
      <header className="col-span-2 flex items-center justify-between px-5 border-b border-border bg-bg-elevated">
        <div className="flex items-center gap-2.5">
          <img src="/app-logo.png" alt="" className="h-7 w-7 object-contain" aria-hidden="true" />
          <span className="font-semibold tracking-tight text-fg">ServiceScout</span>
          <span className="text-fg-dim text-xs ml-1">v0 · alpha</span>
        </div>
        <a
          href="https://github.com/lupuletic/servicescout"
          target="_blank"
          rel="noreferrer"
          className="flex items-center gap-1.5 text-sm text-fg-muted hover:text-fg transition-colors"
        >
          GitHub <ExternalLink size={13} />
        </a>
      </header>

      {/* Sidebar */}
      <nav
        className={cn(
          "border-r border-border bg-bg-elevated/40 py-4 flex flex-col gap-1",
          sidebarCollapsed ? "px-2" : "px-3",
        )}
        aria-label="Primary navigation"
      >
        <button
          type="button"
          onClick={() => setSidebarCollapsed((value) => !value)}
          className={cn(
            "mb-3 flex h-9 items-center rounded-md border border-border text-fg-muted transition-colors hover:border-border-strong hover:bg-bg hover:text-fg",
            sidebarCollapsed ? "justify-center px-0" : "justify-between px-3",
          )}
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {sidebarCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
          {!sidebarCollapsed && <span className="text-xs">Collapse</span>}
        </button>
        {NAV.map(({ to, label, icon: Icon }) => {
          const active = location.pathname === to || (to !== "/" && location.pathname.startsWith(to));
          return (
            <Link
              key={to}
              to={to}
              aria-label={label}
              title={sidebarCollapsed ? label : undefined}
              className={cn(
                "flex h-9 items-center rounded-md text-sm transition-colors",
                sidebarCollapsed ? "justify-center px-0" : "gap-2.5 px-3",
                active
                  ? "bg-accent/15 text-fg border border-accent/30"
                  : "text-fg-muted hover:text-fg hover:bg-bg",
              )}
            >
              <Icon size={sidebarCollapsed ? 18 : 15} />
              {!sidebarCollapsed && <span>{label}</span>}
            </Link>
          );
        })}
      </nav>

      {/* Content */}
      <main className="min-w-0 overflow-auto">{children}</main>
    </div>
  );
}
