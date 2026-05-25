import useSWR, { useSWRConfig } from "swr";
import { type ElementType, type ReactNode, useMemo, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronUp, Database, RotateCw, ShieldCheck, TimerReset, WalletCards } from "lucide-react";
import { type OperatorSummary } from "@/lib/api";
import { Card, CardTitle, CardValue, PageHeader } from "@/components/ui";
import { cn } from "@/lib/cn";

function fmtMoney(value: number) {
  return `$${value.toFixed(value >= 10 ? 0 : 2)}`;
}

function fmtAge(hours?: number | null) {
  if (hours == null) return "-";
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
}

type StaleSortKey = "repo" | "age" | "cost" | "status";
type SortDirection = "asc" | "desc";

function compareNullableNumber(a: number | null | undefined, b: number | null | undefined, direction: SortDirection) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return direction === "asc" ? a - b : b - a;
}

function Sparkline({ rows }: { rows: OperatorSummary["cost_trend"] }) {
  if (rows.length === 0) {
    return <div className="h-28 rounded border border-border bg-bg" />;
  }
  const max = Math.max(...rows.map((r) => r.cost), 0.01);
  const points = rows.map((row, i) => {
    const x = rows.length === 1 ? 50 : (i / (rows.length - 1)) * 100;
    const y = 92 - (row.cost / max) * 76;
    return `${x},${y}`;
  }).join(" ");
  return (
    <svg viewBox="0 0 100 100" role="img" aria-label="Cost trendline" className="h-28 w-full overflow-visible">
      <polyline points={points} fill="none" stroke="rgb(45 212 191)" strokeWidth="3" vectorEffect="non-scaling-stroke" />
      {rows.map((row, i) => {
        const x = rows.length === 1 ? 50 : (i / (rows.length - 1)) * 100;
        const y = 92 - (row.cost / max) * 76;
        return <circle key={row.day} cx={x} cy={y} r="2.4" fill="rgb(226 232 240)" />;
      })}
    </svg>
  );
}

export function OperatorPage() {
  const { data, isLoading } = useSWR<OperatorSummary>("/api/operator/summary", { refreshInterval: 15000 });
  const { mutate } = useSWRConfig();
  const [reindexing, setReindexing] = useState<string | null>(null);
  const [bulkReindexing, setBulkReindexing] = useState(false);
  const [selectedRepos, setSelectedRepos] = useState<Set<string>>(() => new Set());
  const [staleSort, setStaleSort] = useState<{ key: StaleSortKey; direction: SortDirection }>({
    key: "age",
    direction: "desc",
  });
  const trend = data?.cost_trend ?? [];
  const totalTrendCost = trend.reduce((sum, row) => sum + row.cost, 0);
  const buckets = data?.staleness.buckets;
  const staleRows = data?.staleness.repos;
  const staleRepoTotal = data?.staleness.repo_total ?? staleRows?.length ?? 0;
  const sortedStaleRows = useMemo(() => {
    const rows = [...(staleRows ?? [])];
    rows.sort((a, b) => {
      let result = 0;
      if (staleSort.key === "repo") result = a.repo.localeCompare(b.repo);
      if (staleSort.key === "age") result = compareNullableNumber(a.age_hours, b.age_hours, staleSort.direction);
      if (staleSort.key === "cost") result = compareNullableNumber(a.cost, b.cost, staleSort.direction);
      if (staleSort.key === "status") result = a.status.localeCompare(b.status);
      if (result === 0) result = a.repo.localeCompare(b.repo);
      if (staleSort.key === "age" || staleSort.key === "cost") return result;
      return staleSort.direction === "asc" ? result : -result;
    });
    return rows;
  }, [staleRows, staleSort]);
  const selectedSortedRepos = sortedStaleRows.filter((repo) => selectedRepos.has(repo.repo));
  const allSortedReposSelected = sortedStaleRows.length > 0 && selectedSortedRepos.length === sortedStaleRows.length;
  const reindexBusy = reindexing !== null || bulkReindexing;
  const verifierRisk =
    (data?.verifier.validation_errors || 0) +
    (data?.verifier.evidence_quarantined || 0) +
    (data?.verifier.entity_confidence.review || 0);

  const refreshCrawlState = () => Promise.all([mutate("/api/operator/summary"), mutate("/api/crawl/status"), mutate("/api/crawl/runs")]);

  const postReindexRepos = async (repos: string[]) => {
    const response = await fetch("/api/crawl/trigger/repos", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repos }),
    });
    if (!response.ok && response.status !== 202) {
      const body = await response.json().catch(() => ({}));
      alert(body.error ? `Re-index failed: ${body.error}` : `Re-index failed: ${response.status}`);
      return false;
    }
    return true;
  };

  const triggerRepo = async (repo: string) => {
    setReindexing(repo);
    try {
      const ok = await postReindexRepos([repo]);
      if (ok) {
        setSelectedRepos((current) => {
          const next = new Set(current);
          next.delete(repo);
          return next;
        });
      }
      await refreshCrawlState();
    } finally {
      setReindexing(null);
    }
  };

  const triggerSelectedRepos = async () => {
    const repos = selectedSortedRepos.map((repo) => repo.repo);
    if (!repos.length) return;
    setBulkReindexing(true);
    try {
      const ok = await postReindexRepos(repos);
      if (ok) setSelectedRepos(new Set());
      await refreshCrawlState();
    } finally {
      setBulkReindexing(false);
    }
  };

  const toggleRepoSelection = (repo: string) => {
    setSelectedRepos((current) => {
      const next = new Set(current);
      if (next.has(repo)) next.delete(repo);
      else next.add(repo);
      return next;
    });
  };

  const toggleAllSortedRepos = () => {
    if (allSortedReposSelected) {
      setSelectedRepos(new Set());
      return;
    }
    setSelectedRepos(new Set(sortedStaleRows.map((repo) => repo.repo)));
  };

  const sortStaleRows = (key: StaleSortKey) => {
    setStaleSort((current) => ({
      key,
      direction: current.key === key && current.direction === "desc" ? "asc" : "desc",
    }));
  };

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Operator"
        description={data?.catalog.last_build_at ? `Built ${new Date(data.catalog.last_build_at).toLocaleString()}` : "Loading..."}
      />
      <div className="min-w-0 overflow-auto p-4 sm:p-6 space-y-6">
        <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
          <Metric icon={Database} label="Repos" value={data?.catalog.repos_indexed ?? "-"} hint={`${data?.catalog.entities ?? "-"} entities`} />
          <Metric icon={WalletCards} label="30-day cost" value={isLoading ? "-" : fmtMoney(totalTrendCost)} hint={`${trend.reduce((sum, row) => sum + row.repos, 0)} repo runs`} />
          <Metric icon={ShieldCheck} label="Verifier signal" value={verifierRisk} hint={`${data?.verifier.validation_errors ?? "-"} validation errors`} />
          <Metric icon={TimerReset} label="Stale repos" value={buckets?.stale ?? "-"} hint={`${buckets?.aging ?? "-"} aging`} />
        </div>

        <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[minmax(0,1fr)_360px]">
          <Card className="min-w-0">
            <div className="flex items-start justify-between gap-3">
              <div>
                <CardTitle>Cost Trendline</CardTitle>
                <CardValue>{fmtMoney(totalTrendCost)}</CardValue>
              </div>
              <div className="text-right text-xs text-fg-dim">
                {trend[0]?.day || "-"}<br />{trend.at(-1)?.day || "-"}
              </div>
            </div>
            <div className="mt-4">
              <Sparkline rows={trend} />
            </div>
            <div className="mt-3 grid grid-cols-[repeat(auto-fit,minmax(78px,1fr))] gap-2">
              {trend.slice(-5).map((row) => (
                <div key={row.day} className="rounded border border-border bg-bg p-2">
                  <div className="text-[10px] text-fg-dim">{row.day.slice(5)}</div>
                  <div className="text-sm font-medium text-fg">{fmtMoney(row.cost)}</div>
                </div>
              ))}
            </div>
          </Card>

          <Card className="min-w-0">
            <CardTitle>Verifier Signal</CardTitle>
            <div className="space-y-3">
              <SignalRow label="High entities" value={data?.verifier.entity_confidence.high ?? 0} tone="good" />
              <SignalRow label="Medium entities" value={data?.verifier.entity_confidence.medium ?? 0} tone="neutral" />
              <SignalRow label="Review entities" value={data?.verifier.entity_confidence.review ?? 0} tone="warn" />
              <SignalRow label="Quarantined evidence" value={data?.verifier.evidence_quarantined ?? 0} tone="bad" />
              <SignalRow label="Validation errors" value={data?.verifier.validation_errors ?? 0} tone="bad" />
            </div>
          </Card>
        </div>

        <div className="grid grid-cols-1 gap-4 2xl:grid-cols-[340px_minmax(0,1fr)]">
          <Card className="min-w-0">
            <CardTitle>Staleness Heatmap</CardTitle>
            <div className="grid grid-cols-[repeat(auto-fit,minmax(86px,1fr))] gap-2">
              {(["fresh", "warm", "aging", "stale", "unknown"] as const).map((bucket) => (
                <div
                  key={bucket}
                  className={cn(
                    "rounded border p-3",
                    bucket === "fresh" && "border-green-500/30 bg-green-500/10",
                    bucket === "warm" && "border-blue-500/30 bg-blue-500/10",
                    bucket === "aging" && "border-yellow-500/30 bg-yellow-500/10",
                    bucket === "stale" && "border-red-500/30 bg-red-500/10",
                    bucket === "unknown" && "border-border bg-bg",
                  )}
                >
                  <div className="text-[10px] uppercase tracking-wider text-fg-dim">{bucket}</div>
                  <div className="mt-1 text-xl font-semibold text-fg">{buckets?.[bucket] ?? 0}</div>
                </div>
              ))}
            </div>
          </Card>

          <Card className="min-w-0">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <div className="flex items-center gap-2">
                  <CardTitle>Stale Repo Queue</CardTitle>
                  <AlertTriangle size={15} className="text-fg-dim" />
                </div>
                <div className="mt-1 text-xs text-fg-dim">
                  Showing {sortedStaleRows.length} of {staleRepoTotal} repos due for re-index. Stale means last extraction is older than 7 days.
                </div>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                <span className="text-xs text-fg-dim">{selectedSortedRepos.length} selected</span>
                <button
                  type="button"
                  onClick={toggleAllSortedRepos}
                  disabled={sortedStaleRows.length === 0 || reindexBusy}
                  className="rounded border border-border px-2 py-1 text-xs text-fg-muted hover:text-fg disabled:opacity-50"
                >
                  {allSortedReposSelected ? "Deselect all" : "Select all"}
                </button>
                <button
                  type="button"
                  onClick={() => setSelectedRepos(new Set())}
                  disabled={selectedSortedRepos.length === 0 || reindexBusy}
                  className="rounded border border-border px-2 py-1 text-xs text-fg-muted hover:text-fg disabled:opacity-50"
                >
                  Clear
                </button>
                <button
                  type="button"
                  onClick={() => void triggerSelectedRepos()}
                  disabled={selectedSortedRepos.length === 0 || reindexBusy}
                  className="inline-flex items-center gap-1 rounded border border-accent/50 bg-accent/15 px-2 py-1 text-xs font-medium text-accent hover:bg-accent/20 disabled:opacity-50"
                >
                  <RotateCw size={12} className={bulkReindexing ? "animate-spin" : ""} />
                  Re-index selected
                </button>
              </div>
            </div>
            <div className="mt-3 max-h-[520px] overflow-auto rounded border border-border">
              <div className="min-w-[680px]">
                <div className="sticky top-0 z-10 grid grid-cols-[38px_minmax(220px,1fr)_86px_80px_80px_92px] gap-3 bg-bg px-3 py-2 text-xs uppercase tracking-wider text-fg-dim">
                  <div>
                    <input
                      type="checkbox"
                      aria-label="Select all stale repositories"
                      checked={allSortedReposSelected}
                      disabled={sortedStaleRows.length === 0 || reindexBusy}
                      onChange={toggleAllSortedRepos}
                      className="h-4 w-4 accent-accent"
                    />
                  </div>
                  <SortHeader label="Repo" sortKey="repo" sort={staleSort} onSort={sortStaleRows} />
                  <SortHeader label="Age" sortKey="age" sort={staleSort} onSort={sortStaleRows} />
                  <SortHeader label="Cost" sortKey="cost" sort={staleSort} onSort={sortStaleRows} />
                  <SortHeader label="Status" sortKey="status" sort={staleSort} onSort={sortStaleRows} />
                  <div className="text-right">Action</div>
                </div>
                {sortedStaleRows.length === 0 && (
                  <div className="border-t border-border px-3 py-6 text-sm text-fg-dim">
                    No stale repositories in the queue.
                  </div>
                )}
                {sortedStaleRows.map((repo) => (
                  <div key={repo.repo} className="grid grid-cols-[38px_minmax(220px,1fr)_86px_80px_80px_92px] gap-3 border-t border-border px-3 py-2 text-sm">
                    <div>
                      <input
                        type="checkbox"
                        aria-label={`Select ${repo.repo}`}
                        checked={selectedRepos.has(repo.repo)}
                        disabled={reindexBusy}
                        onChange={() => toggleRepoSelection(repo.repo)}
                        className="h-4 w-4 accent-accent"
                      />
                    </div>
                    <div className="truncate font-mono text-xs text-fg">{repo.repo}</div>
                    <div className="text-fg-muted">{fmtAge(repo.age_hours)}</div>
                    <div className="text-fg-muted">{fmtMoney(repo.cost)}</div>
                    <div className="truncate text-fg-muted">{repo.status}</div>
                    <div className="text-right">
                      <button
                        type="button"
                        onClick={() => void triggerRepo(repo.repo)}
                        disabled={reindexBusy}
                        className="inline-flex items-center gap-1 rounded border border-border px-2 py-1 text-xs text-fg-muted hover:text-fg disabled:opacity-50"
                      >
                        <RotateCw size={12} className={reindexing === repo.repo ? "animate-spin" : ""} />
                        Re-index
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}

function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
}: {
  label: string;
  sortKey: StaleSortKey;
  sort: { key: StaleSortKey; direction: SortDirection };
  onSort: (key: StaleSortKey) => void;
}) {
  const active = sort.key === sortKey;
  const Icon = sort.direction === "asc" ? ChevronUp : ChevronDown;
  return (
    <button
      type="button"
      onClick={() => onSort(sortKey)}
      className={cn("inline-flex items-center gap-1 text-left uppercase tracking-wider hover:text-fg", active ? "text-fg" : "text-fg-dim")}
      aria-label={`Sort stale repositories by ${label}`}
    >
      {label}
      {active && <Icon size={12} aria-hidden="true" />}
    </button>
  );
}

function Metric({ icon: Icon, label, value, hint }: { icon: ElementType; label: string; value: ReactNode; hint?: string }) {
  return (
    <Card>
      <div className="flex items-center justify-between">
        <CardTitle>{label}</CardTitle>
        <Icon size={16} className="text-fg-dim" />
      </div>
      <CardValue>{value}</CardValue>
      {hint && <div className="text-xs text-fg-dim mt-1">{hint}</div>}
    </Card>
  );
}

function SignalRow({ label, value, tone }: { label: string; value: number; tone: "good" | "neutral" | "warn" | "bad" }) {
  return (
    <div className="flex items-center justify-between rounded border border-border bg-bg px-3 py-2 text-sm">
      <span className="text-fg-muted">{label}</span>
      <span
        className={cn(
          "font-semibold",
          tone === "good" && "text-green-400",
          tone === "neutral" && "text-blue-400",
          tone === "warn" && "text-yellow-400",
          tone === "bad" && "text-red-400",
        )}
      >
        {value}
      </span>
    </div>
  );
}
