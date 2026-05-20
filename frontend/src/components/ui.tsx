// Minimal shadcn-inspired primitives. Owned in-repo — no external dep, easy to
// re-style. If we need anything fancier (combobox, popover, sheet), add via
// `npx shadcn@latest add` later.

import { type ReactNode, type HTMLAttributes, type ButtonHTMLAttributes, type InputHTMLAttributes, forwardRef } from "react";
import { cn } from "@/lib/cn";
import { kindLabel } from "@/lib/catalogLabels";

export const Card = ({ className, ...props }: HTMLAttributes<HTMLDivElement>) => (
  <div
    className={cn(
      "rounded-lg border border-border bg-bg-elevated p-4",
      className,
    )}
    {...props}
  />
);

export const CardTitle = ({ className, ...props }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("text-xs uppercase tracking-wider text-fg-dim mb-2", className)} {...props} />
);

export const CardValue = ({ className, ...props }: HTMLAttributes<HTMLDivElement>) => (
  <div className={cn("text-2xl font-semibold text-fg", className)} {...props} />
);

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "ghost" | "outline" }>(
  ({ className, variant = "default", ...props }, ref) => (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus:outline-none focus:ring-1 focus:ring-accent disabled:opacity-50 disabled:pointer-events-none",
        variant === "default" && "bg-accent text-accent-fg hover:bg-accent/90",
        variant === "ghost" && "text-fg-muted hover:text-fg hover:bg-bg-elevated",
        variant === "outline" && "border border-border text-fg hover:border-border-strong hover:bg-bg-elevated",
        className,
      )}
      {...props}
    />
  ),
);
Button.displayName = "Button";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        "flex h-9 w-full rounded-md border border-border bg-bg-elevated px-3 py-1 text-sm text-fg placeholder:text-fg-dim focus:outline-none focus:ring-1 focus:ring-accent focus:border-accent",
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = "Input";

const KIND_COLORS: Record<string, string> = {
  Component: "bg-component/15 text-component border-component/30",
  API: "bg-api/15 text-api border-api/30",
  Resource: "bg-resource/15 text-resource border-resource/30",
  Provider: "bg-provider/15 text-provider border-provider/30",
  System: "bg-system/15 text-system border-system/30",
  Domain: "bg-domain/15 text-domain border-domain/30",
  Group: "bg-group/15 text-group border-group/30",
};

const CONFIDENCE_COLORS: Record<string, string> = {
  high: "bg-green-500/15 text-green-400 border-green-500/30",
  medium: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  low: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30",
  review: "bg-red-500/15 text-red-400 border-red-500/30",
};

export function KindBadge({ kind, className }: { kind: string; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        KIND_COLORS[kind] || "bg-bg-elevated text-fg-muted border-border",
        className,
      )}
    >
      {kindLabel(kind)}
    </span>
  );
}

export function ConfidenceBadge({ confidence, className }: { confidence?: string; className?: string }) {
  const value = confidence || "review";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        CONFIDENCE_COLORS[value] || "bg-bg-elevated text-fg-muted border-border",
        className,
      )}
    >
      {value}
    </span>
  );
}

const STATUS_COLORS: Record<string, string> = {
  ok: "bg-green-500/15 text-green-400 border-green-500/30",
  no_changes: "bg-blue-500/15 text-blue-400 border-blue-500/30",
  tick_skipped_busy: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30",
  crawler_failed: "bg-red-500/15 text-red-400 border-red-500/30",
  exception: "bg-red-500/15 text-red-400 border-red-500/30",
  running: "bg-accent/15 text-accent border-accent/30",
};

export function StatusBadge({ status, className }: { status: string; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        STATUS_COLORS[status] || "bg-bg text-fg-muted border-border",
        className,
      )}
    >
      {status.replaceAll("_", " ")}
    </span>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <Card>
      <CardTitle>{label}</CardTitle>
      <CardValue>{value}</CardValue>
      {hint && <div className="text-xs text-fg-dim mt-1">{hint}</div>}
    </Card>
  );
}

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <div className="flex items-start justify-between border-b border-border bg-bg-elevated/40 px-6 py-4">
      <div>
        <h1 className="text-lg font-semibold tracking-tight text-fg">{title}</h1>
        {description && <p className="text-sm text-fg-muted mt-0.5">{description}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}
