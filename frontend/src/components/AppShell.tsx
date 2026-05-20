import { type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";
import { Network, Boxes, ListChecks, ExternalLink, Gauge, CalendarClock } from "lucide-react";
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
  return (
    <div className="grid grid-cols-[220px_1fr] grid-rows-[56px_1fr] h-screen">
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
      <nav className="border-r border-border bg-bg-elevated/40 py-4 px-3 flex flex-col gap-1">
        {NAV.map(({ to, label, icon: Icon }) => {
          const active = location.pathname === to || (to !== "/" && location.pathname.startsWith(to));
          return (
            <Link
              key={to}
              to={to}
              className={cn(
                "flex items-center gap-2.5 px-3 py-2 rounded-md text-sm transition-colors",
                active
                  ? "bg-accent/15 text-fg border border-accent/30"
                  : "text-fg-muted hover:text-fg hover:bg-bg",
              )}
            >
              <Icon size={15} />
              {label}
            </Link>
          );
        })}
        <div className="mt-auto pt-4 border-t border-border text-xs text-fg-dim px-3">
          <p>Open-source MCP server</p>
          <p className="mt-1">for AI coding agents</p>
        </div>
      </nav>

      {/* Content */}
      <main className="overflow-auto">{children}</main>
    </div>
  );
}
