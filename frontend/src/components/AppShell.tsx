import { type ReactNode, useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  Network,
  Boxes,
  ListChecks,
  ExternalLink,
  Gauge,
  CalendarClock,
  Check,
  Clipboard,
  PanelLeftClose,
  PanelLeftOpen,
  Rocket,
  TerminalSquare,
} from "lucide-react";
import useSWR from "swr";
import type { StatusPayload } from "@/lib/api";
import { cn } from "@/lib/cn";

// Onboarding is intentionally NOT a primary nav item — it's the first-run
// empty state and the seed-enrichment action, not a place you live in.
const NAV = [
  { to: "/", label: "Explorer", icon: Network },
  { to: "/entities", label: "Catalog", icon: Boxes },
  { to: "/activity", label: "Activity", icon: CalendarClock },
  { to: "/operator", label: "Operator", icon: Gauge },
  { to: "/triage", label: "Triage", icon: ListChecks },
];

export function AppShell({ children }: { children: ReactNode }) {
  const location = useLocation();
  const { data: state } = useSWR<StatusPayload>("/api/state.json");
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem("servicescout:sidebar-collapsed") === "true";
  });
  const [setupOpen, setSetupOpen] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    window.localStorage.setItem("servicescout:sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  const deployment = state?.deployment;
  const mcpUrl = deployment?.mcp_url || (typeof window === "undefined" ? "" : `${window.location.origin}/mcp`);
  const setupCommands = [
    ["Both", deployment?.install_command || `curl -fsSL https://raw.githubusercontent.com/lupuletic/servicescout/main/install.sh | bash -s -- --agent both --url ${mcpUrl} --yes`],
    ["Claude", deployment?.claude_command || `claude mcp add servicescout --scope user --transport http ${mcpUrl}`],
    ["Codex", deployment?.codex_command || `codex mcp add servicescout --url ${mcpUrl}`],
  ] as const;

  async function copyCommand(label: string, command: string) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(command);
    } else {
      const input = document.createElement("textarea");
      input.value = command;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    }
    setCopied(label);
    window.setTimeout(() => setCopied((current) => (current === label ? null : current)), 1600);
  }

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
        <div className="flex items-center gap-4">
          <div className="relative">
            <button
              type="button"
              onClick={() => setSetupOpen((value) => !value)}
              className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-sm text-fg-muted transition-colors hover:border-border-strong hover:bg-bg hover:text-fg"
              aria-expanded={setupOpen}
              title="Remote MCP setup"
            >
              <TerminalSquare size={14} /> MCP
            </button>
            {setupOpen && (
              <div className="absolute right-0 top-9 z-50 w-[min(560px,calc(100vw-24px))] rounded-md border border-border bg-bg-elevated p-3 shadow-2xl">
                <div className="mb-2 flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-fg">Remote MCP</div>
                    <div className="truncate font-mono text-xs text-fg-dim">{mcpUrl}</div>
                  </div>
                  <a
                    href={mcpUrl}
                    target="_blank"
                    rel="noreferrer"
                    className="shrink-0 text-fg-dim transition-colors hover:text-fg"
                    title="Open MCP endpoint"
                    aria-label="Open MCP endpoint"
                  >
                    <ExternalLink size={15} />
                  </a>
                </div>
                <div className="space-y-2">
                  {setupCommands.map(([label, command]) => (
                    <div key={label} className="grid grid-cols-[54px_minmax(0,1fr)_32px] items-center gap-2 rounded border border-border bg-bg px-2 py-1.5">
                      <span className="text-xs font-medium text-fg-muted">{label}</span>
                      <code className="min-w-0 truncate font-mono text-xs text-fg">{command}</code>
                      <button
                        type="button"
                        onClick={() => copyCommand(label, command)}
                        className="flex h-7 w-7 items-center justify-center rounded text-fg-muted transition-colors hover:bg-bg-elevated hover:text-fg"
                        title={`Copy ${label} command`}
                        aria-label={`Copy ${label} command`}
                      >
                        {copied === label ? <Check size={14} /> : <Clipboard size={14} />}
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
          <Link
            to="/onboard"
            className="flex items-center gap-1.5 rounded-md border border-accent/40 px-2.5 py-1 text-sm text-fg hover:bg-accent/15 transition-colors"
          >
            <Rocket size={14} /> Add seed
          </Link>
          <a
            href="https://github.com/lupuletic/servicescout"
            target="_blank"
            rel="noreferrer"
            className="flex items-center gap-1.5 text-sm text-fg-muted hover:text-fg transition-colors"
          >
            GitHub <ExternalLink size={13} />
          </a>
        </div>
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
