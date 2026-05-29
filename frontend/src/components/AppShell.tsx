import { type ReactNode, useEffect, useRef, useState } from "react";
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
  const setupMenuRefs = useRef<Array<HTMLDivElement | null>>([]);

  useEffect(() => {
    window.localStorage.setItem("servicescout:sidebar-collapsed", String(sidebarCollapsed));
  }, [sidebarCollapsed]);

  useEffect(() => {
    if (!setupOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSetupOpen(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;

      const isInsideSetupMenu = setupMenuRefs.current.some((node) => node?.contains(target));
      if (!isInsideSetupMenu) setSetupOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("pointerdown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("pointerdown", onPointerDown);
    };
  }, [setupOpen]);

  const deployment = state?.deployment;
  const mcpUrl = deployment?.mcp_url || (typeof window === "undefined" ? "" : `${window.location.origin}/mcp`);
  const setupCommands = [
    ["Both", deployment?.install_command || `curl -fsSL https://raw.githubusercontent.com/lupuletic/servicescout/main/install.sh | bash -s -- --agent both --url ${mcpUrl} --yes`],
    ["Claude", deployment?.claude_command || `claude mcp add servicescout --scope user --transport http ${mcpUrl}`],
    ["Codex", deployment?.codex_command || `codex mcp add servicescout --url ${mcpUrl}`],
  ] as const;

  async function copyCommand(label: string, command: string) {
    const fallbackCopy = () => {
      const input = document.createElement("textarea");
      input.value = command;
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.appendChild(input);
      input.select();
      document.execCommand("copy");
      input.remove();
    };
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(command);
      } catch {
        fallbackCopy();
      }
    } else {
      fallbackCopy();
    }
    setCopied(label);
    window.setTimeout(() => setCopied((current) => (current === label ? null : current)), 1600);
  }

  const renderMcpSetupMenu = (compact = false, menuIndex = 0) => (
      <div
        ref={(node) => {
          setupMenuRefs.current[menuIndex] = node;
        }}
        className="relative"
      >
        <button
          type="button"
          onClick={() => setSetupOpen((value) => !value)}
          className={cn(
            "flex h-9 items-center rounded-md border border-border text-sm text-fg-muted transition-colors hover:border-border-strong hover:bg-bg hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70",
            compact || sidebarCollapsed ? "w-10 justify-center lg:w-9" : "w-full gap-2.5 px-3",
          )}
          aria-expanded={setupOpen}
          title="Remote MCP setup"
        >
          <TerminalSquare size={compact || sidebarCollapsed ? 16 : 15} />
          {!compact && !sidebarCollapsed && <span>Remote MCP</span>}
        </button>
        {setupOpen && (
          <div
            className={cn(
              "absolute z-50 w-[min(560px,calc(100vw-24px))] rounded-md border border-border bg-bg-elevated p-3 shadow-[0_18px_40px_rgba(0,0,0,0.38)]",
              compact
                ? "right-0 top-11"
                : sidebarCollapsed
                  ? "bottom-0 left-12"
                  : "bottom-11 left-0",
            )}
          >
            <div className="mb-2 flex items-center justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm font-medium text-fg">Remote MCP setup</div>
                <div className="truncate font-mono text-xs text-fg-dim">{mcpUrl}</div>
              </div>
              <a
                href={mcpUrl}
                target="_blank"
                rel="noreferrer"
                className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-fg-dim transition-colors hover:bg-bg hover:text-fg"
                title="Open MCP endpoint"
                aria-label="Open MCP endpoint"
              >
                <ExternalLink size={15} />
              </a>
            </div>
            <div className="space-y-2">
              {setupCommands.map(([label, command]) => (
                <div key={label} className="grid grid-cols-[54px_minmax(0,1fr)_36px] items-center gap-2 rounded-md border border-border bg-bg px-2 py-1.5">
                  <span className="text-xs font-medium text-fg-muted">{label}</span>
                  <code className="min-w-0 truncate font-mono text-xs text-fg">{command}</code>
                  <button
                    type="button"
                    onClick={() => copyCommand(label, command)}
                    className="grid h-8 w-8 place-items-center rounded-md text-fg-muted transition-colors hover:bg-bg-elevated hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70"
                    title={`Copy ${label} command`}
                    aria-label={`Copy ${label} command`}
                  >
                    {copied === label ? <Check size={14} /> : <Clipboard size={14} />}
                  </button>
                </div>
              ))}
            </div>
            <div className="mt-2 min-h-4 text-xs text-accent" role="status" aria-live="polite">
              {copied ? `${copied} command copied` : ""}
            </div>
          </div>
        )}
      </div>
  );

  return (
    <div
      className={cn(
        "grid h-screen grid-cols-1 grid-rows-[56px_auto_minmax(0,1fr)] lg:grid-rows-[56px_minmax(0,1fr)]",
        sidebarCollapsed ? "lg:grid-cols-[64px_minmax(0,1fr)]" : "lg:grid-cols-[220px_minmax(0,1fr)]",
      )}
    >
      {/* Top bar */}
      <header className="flex items-center justify-between gap-3 border-b border-border bg-bg-elevated px-4 sm:px-5 lg:col-span-2">
        <div className="flex min-w-0 items-center gap-2.5">
          <img src="/app-logo.png" alt="" className="h-7 w-7 object-contain" aria-hidden="true" />
          <span className="truncate font-semibold tracking-tight text-fg">ServiceScout</span>
          <span className="ml-1 shrink-0 text-xs text-fg-dim">v0 · alpha</span>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <div className="lg:hidden">
            {renderMcpSetupMenu(true, 0)}
          </div>
          <Link
            to="/onboard"
            className="flex h-10 items-center gap-1.5 rounded-md border border-accent/40 px-2.5 text-sm text-fg transition-colors hover:bg-accent/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 lg:hidden"
          >
            <Rocket size={14} />
            <span className="hidden sm:inline">Add seed</span>
          </Link>
          <a
            href="https://github.com/lupuletic/servicescout"
            target="_blank"
            rel="noreferrer"
            className="flex h-10 w-10 items-center justify-center gap-1.5 rounded-md px-2 text-sm text-fg-muted transition-colors hover:bg-bg hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 sm:w-auto"
            aria-label="Open ServiceScout on GitHub"
          >
            <span className="hidden sm:inline">GitHub</span>
            <ExternalLink size={14} />
          </a>
        </div>
      </header>

      {/* Sidebar */}
      <nav
        className={cn(
          "flex min-w-0 items-center gap-1 border-b border-border bg-bg-elevated/40 px-2 py-2 lg:flex-col lg:items-stretch lg:border-b-0 lg:border-r lg:py-4",
          sidebarCollapsed ? "lg:px-2" : "lg:px-3",
        )}
        aria-label="Primary navigation"
      >
        <button
          type="button"
          onClick={() => setSidebarCollapsed((value) => !value)}
          className={cn(
            "mb-3 hidden h-9 items-center rounded-md border border-border text-fg-muted transition-colors hover:border-border-strong hover:bg-bg hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 lg:flex",
            sidebarCollapsed ? "justify-center px-0" : "justify-between px-3",
          )}
          aria-label={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={sidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {sidebarCollapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
          {!sidebarCollapsed && <span className="text-xs">Collapse</span>}
        </button>
        <div className="flex min-w-0 flex-1 gap-1 overflow-x-auto lg:flex-none lg:flex-col lg:overflow-visible">
          {NAV.map(({ to, label, icon: Icon }) => {
            const active = location.pathname === to || (to !== "/" && location.pathname.startsWith(to));
            return (
              <Link
                key={to}
                to={to}
                aria-label={label}
                title={sidebarCollapsed ? label : undefined}
                className={cn(
                  "flex h-9 shrink-0 items-center rounded-md border text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70",
                  sidebarCollapsed ? "justify-center px-0 lg:w-11" : "gap-2.5 px-3",
                  active
                    ? "border-accent/30 bg-accent/15 text-fg"
                    : "border-transparent text-fg-muted hover:bg-bg hover:text-fg",
                )}
              >
                <Icon size={sidebarCollapsed ? 18 : 15} />
                {!sidebarCollapsed && <span>{label}</span>}
              </Link>
            );
          })}
        </div>

        <div className="mt-auto hidden border-t border-border pt-3 lg:flex lg:flex-col lg:gap-1">
          {renderMcpSetupMenu(false, 1)}
          <Link
            to="/onboard"
            className={cn(
              "flex h-9 items-center rounded-md border border-accent/35 text-sm text-fg transition-colors hover:bg-accent/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70",
              sidebarCollapsed ? "justify-center px-0" : "gap-2.5 px-3",
            )}
            title={sidebarCollapsed ? "Add seed" : undefined}
            aria-label="Add seed"
          >
            <Rocket size={sidebarCollapsed ? 18 : 15} />
            {!sidebarCollapsed && <span>Add seed</span>}
          </Link>
        </div>
      </nav>

      {/* Content */}
      <main className="min-w-0 overflow-auto lg:col-start-2 lg:row-start-2">{children}</main>
    </div>
  );
}
