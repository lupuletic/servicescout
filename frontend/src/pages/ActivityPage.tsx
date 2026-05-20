import { useMemo, useState, type ReactNode } from "react";
import useSWR, { useSWRConfig } from "swr";
import { AlertCircle, CheckCircle2, Clock3, GitBranch, Loader2, Play, RefreshCw, XCircle } from "lucide-react";
import { type CrawlRunDetail, type CrawlRunsPayload, type CrawlStatusPayload } from "@/lib/api";
import { Button, Card, CardTitle, CardValue, PageHeader, StatusBadge } from "@/components/ui";
import { cn } from "@/lib/cn";

function StatusGlyph({ status, className }: { status?: string; className?: string }) {
  if (status === "ok" || status === "no_changes") return <CheckCircle2 size={14} className={className} />;
  if (status === "crawler_failed" || status === "exception") return <XCircle size={14} className={className} />;
  if (status === "tick_skipped_busy") return <Clock3 size={14} className={className} />;
  return <AlertCircle size={14} className={className} />;
}

function formatDate(value?: string | null) {
  if (!value) return "-";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function duration(start?: string, finish?: string) {
  if (!start || !finish) return "-";
  const seconds = Math.max((new Date(finish).getTime() - new Date(start).getTime()) / 1000, 0);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.round(seconds / 60)}m`;
}

function fmtMoney(value?: number | null) {
  if (value == null) return "-";
  return `$${value.toFixed(value >= 10 ? 0 : 2)}`;
}

export function ActivityPage() {
  const { data: status } = useSWR<CrawlStatusPayload>("/api/crawl/status", { refreshInterval: 3000 });
  const { data: runs, isLoading } = useSWR<CrawlRunsPayload>("/api/crawl/runs", { refreshInterval: 5000 });
  const { mutate } = useSWRConfig();
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [triggering, setTriggering] = useState(false);
  const selected = selectedRun || runs?.runs?.[0]?.run_id || null;
  const { data: detail } = useSWR<CrawlRunDetail>(selected ? `/api/crawl/runs/${selected}` : null);

  const triggerNow = async () => {
    setTriggering(true);
    try {
      const response = await fetch("/api/crawl/trigger", { method: "POST" });
      if (!response.ok && response.status !== 202) {
        const body = await response.json().catch(() => ({}));
        alert(body.error ? `Trigger failed: ${body.error}` : `Trigger failed: ${response.status}`);
      }
      await Promise.all([mutate("/api/crawl/status"), mutate("/api/crawl/runs")]);
    } finally {
      setTriggering(false);
    }
  };

  const totalChanged = useMemo(
    () => (runs?.runs || []).reduce((sum, run) => sum + (run.repos_changed_count || 0), 0),
    [runs],
  );
  const recentRunsLabel = runs?.source === "extractions" ? "extraction runs" : "listed in Activity";
  const changedLabel = runs?.source === "extractions" ? "repos extracted" : "recent window";

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Activity"
        description={status?.lock?.held ? "Scheduler tick running" : `Every ${status?.interval_minutes ?? "-"} minutes`}
        actions={
          <>
            <Button variant="outline" onClick={() => void mutate(() => true)}>
              <RefreshCw size={14} /> Refresh
            </Button>
            <Button onClick={triggerNow} disabled={triggering || status?.lock?.held}>
              {triggering ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              Trigger now
            </Button>
          </>
        }
      />
      <div className="grid grid-cols-[minmax(0,1fr)_420px] min-h-0">
        <div className="overflow-auto p-6 space-y-5">
          <div className="grid grid-cols-4 gap-3">
            <Card>
              <CardTitle>Scheduler</CardTitle>
              <CardValue>{status?.lock?.held ? "Running" : "Idle"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">{status?.lock?.pid ? `PID ${status.lock.pid}` : "Ready"}</div>
            </Card>
            <Card>
              <CardTitle>Tick Budget</CardTitle>
              <CardValue>${status?.budget_usd?.toFixed(0) ?? "-"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">per run</div>
            </Card>
            <Card>
              <CardTitle>Recent Runs</CardTitle>
              <CardValue>{runs?.total ?? "-"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">{recentRunsLabel}</div>
            </Card>
            <Card>
              <CardTitle>Changed Repos</CardTitle>
              <CardValue>{totalChanged}</CardValue>
              <div className="text-xs text-fg-dim mt-1">{changedLabel}</div>
            </Card>
          </div>

          <section className="rounded-lg border border-border bg-bg-elevated overflow-hidden">
            <div className="grid grid-cols-[140px_minmax(220px,1fr)_100px_80px_90px_85px] gap-3 px-4 py-2 text-xs uppercase tracking-wider text-fg-dim border-b border-border">
              <div>Started</div>
              <div>Run</div>
              <div>Status</div>
              <div>Changed</div>
              <div className="text-right">Cost</div>
              <div className="text-right">Duration</div>
            </div>
            {isLoading && <div className="p-6 text-fg-muted">Loading runs...</div>}
            {!isLoading && (runs?.runs || []).length === 0 && (
              <div className="p-6 text-fg-muted">No activity recorded.</div>
            )}
            {(runs?.runs || []).map((run) => {
              const active = selected === run.run_id;
              return (
                <button
                  key={run.run_id}
                  onClick={() => setSelectedRun(run.run_id)}
                  className={cn(
                    "grid w-full grid-cols-[140px_minmax(220px,1fr)_100px_80px_90px_85px] gap-3 px-4 py-3 text-left text-sm border-b border-border last:border-b-0 hover:bg-bg",
                    active && "bg-bg",
                  )}
                >
                  <div className="text-fg-muted">{formatDate(run.started_at)}</div>
                  <div className="min-w-0">
                    <div className="font-mono text-xs text-fg truncate">{run.run_id}</div>
                    <div className="text-xs text-fg-dim">{run.trigger}</div>
                  </div>
                  <div className="flex items-center gap-1.5 text-fg-muted">
                    <StatusGlyph status={run.status} />
                    <StatusBadge status={run.status || "unknown"} />
                  </div>
                  <div className="text-fg-muted">{run.repos_changed_count}</div>
                  <div className="text-right text-fg-muted tabular-nums">{fmtMoney(run.cost_usd)}</div>
                  <div className="text-right text-fg-muted">{duration(run.started_at, run.finished_at)}</div>
                </button>
              );
            })}
          </section>
        </div>

        <aside className="border-l border-border bg-bg-elevated overflow-auto">
          {!detail && <div className="p-6 text-sm text-fg-muted">Select a run.</div>}
          {detail && <RunDetail detail={detail} />}
        </aside>
      </div>
    </div>
  );
}

function RunDetail({ detail }: { detail: CrawlRunDetail }) {
  return (
    <div className="p-6 space-y-5 text-sm">
      <div>
        <div className="flex items-center gap-2">
          <StatusGlyph status={detail.status} className="text-fg-muted" />
          <StatusBadge status={detail.status || "unknown"} />
        </div>
        <h2 className="mt-2 font-mono text-sm text-fg break-all">{detail.run_id}</h2>
        <div className="mt-1 text-xs text-fg-dim">
          {formatDate(detail.started_at)} - {formatDate(detail.finished_at)}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <MiniMetric label="Checked" value={detail.repos_checked ?? "-"} />
        <MiniMetric label="Changed" value={detail.repos_changed?.length ?? detail.repos_changed_count ?? "-"} />
        <MiniMetric label={detail.trigger === "extraction" ? "Cost" : "Budget"} value={detail.trigger === "extraction" ? fmtMoney(detail.cost_usd) : detail.budget_usd != null ? `$${detail.budget_usd}` : "-"} />
        <MiniMetric label="Return" value={detail.crawler_returncode ?? "-"} />
      </div>

      {(detail.repos_changed || []).length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-wider text-fg-dim mb-2">Changed repos</h3>
          <div className="space-y-1">
            {(detail.repos_changed || []).slice(0, 12).map((repo) => (
              <div key={repo.repo} className="rounded border border-border bg-bg px-3 py-2">
                <div className="flex items-center gap-2 text-fg">
                  <GitBranch size={13} className="text-fg-dim" />
                  <span className="font-mono text-xs truncate">{repo.repo}</span>
                </div>
                <div className="mt-1 text-xs text-fg-dim">{repo.reason || "changed"}</div>
              </div>
            ))}
          </div>
        </section>
      )}

      {(detail.events || []).length > 0 && (
        <section>
          <h3 className="text-xs uppercase tracking-wider text-fg-dim mb-2">Timeline</h3>
          <ol className="space-y-2">
            {(detail.events || []).map((event, index) => (
              <li key={index} className="rounded border border-border bg-bg px-3 py-2">
                <div className="font-mono text-xs text-fg">{String(event.event || "event")}</div>
                <div className="mt-1 text-xs text-fg-dim truncate">{JSON.stringify(event)}</div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {(detail.crawler_stdout_tail || detail.crawler_stderr_tail || detail.error) && (
        <section>
          <h3 className="text-xs uppercase tracking-wider text-fg-dim mb-2">Output</h3>
          <pre className="max-h-72 overflow-auto rounded border border-border bg-bg p-3 text-xs text-fg-muted whitespace-pre-wrap">
            {detail.error || detail.crawler_stderr_tail || detail.crawler_stdout_tail}
          </pre>
        </section>
      )}
    </div>
  );
}

function MiniMetric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded border border-border bg-bg p-3">
      <div className="text-xs uppercase tracking-wider text-fg-dim">{label}</div>
      <div className="mt-1 text-lg font-semibold text-fg">{value}</div>
    </div>
  );
}
