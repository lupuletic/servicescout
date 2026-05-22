import { useEffect, useMemo, useState, type ReactNode } from "react";
import useSWR, { useSWRConfig } from "swr";
import { AlertCircle, CheckCircle2, Clock3, GitBranch, Loader2, Pause, Play, RefreshCw, RotateCw, Settings2, XCircle } from "lucide-react";
import { type CrawlRunDetail, type CrawlRunsPayload, type CrawlStatusPayload } from "@/lib/api";
import { Button, Card, CardTitle, CardValue, Input, PageHeader, StatusBadge } from "@/components/ui";
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

function formatInterval(minutes?: number) {
  if (!minutes) return "-";
  if (minutes < 60) return `${minutes}m`;
  const hours = minutes / 60;
  return Number.isInteger(hours) ? `${minutes}m (${hours}h)` : `${minutes}m`;
}

export function ActivityPage() {
  const { data: status } = useSWR<CrawlStatusPayload>("/api/crawl/status", { refreshInterval: 3000 });
  const { data: runs, isLoading } = useSWR<CrawlRunsPayload>("/api/crawl/runs", { refreshInterval: 5000 });
  const { mutate } = useSWRConfig();
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [triggering, setTriggering] = useState(false);
  const [automationAction, setAutomationAction] = useState<"start" | "stop" | null>(null);
  const [runtimeAction, setRuntimeAction] = useState(false);
  const [intervalInput, setIntervalInput] = useState("360");
  const [budgetInput, setBudgetInput] = useState("20");
  const [parallelismInput, setParallelismInput] = useState("8");
  const [batchSizeInput, setBatchSizeInput] = useState("24");
  const selected = selectedRun || runs?.runs?.[0]?.run_id || null;
  const { data: detail } = useSWR<CrawlRunDetail>(selected ? `/api/crawl/runs/${selected}` : null);
  const schedulerInterval = status?.scheduler?.interval_minutes ?? status?.interval_minutes;
  const schedulerBudget = status?.scheduler?.budget_usd ?? status?.budget_usd;

  useEffect(() => {
    if (automationAction) return;
    if (schedulerInterval != null) setIntervalInput(String(schedulerInterval));
    if (schedulerBudget != null) setBudgetInput(String(schedulerBudget));
  }, [automationAction, schedulerBudget, schedulerInterval]);

  useEffect(() => {
    if (runtimeAction) return;
    if (status?.crawler_runtime?.parallelism != null) setParallelismInput(String(status.crawler_runtime.parallelism));
    if (status?.crawler_runtime?.batch_size != null) setBatchSizeInput(String(status.crawler_runtime.batch_size));
  }, [runtimeAction, status?.crawler_runtime?.batch_size, status?.crawler_runtime?.parallelism]);

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

  const triggerRepo = async (repo: string) => {
    const response = await fetch(`/api/crawl/trigger/repo?repo=${encodeURIComponent(repo)}`, { method: "POST" });
    if (!response.ok && response.status !== 202) {
      const body = await response.json().catch(() => ({}));
      alert(body.error ? `Re-index failed: ${body.error}` : `Re-index failed: ${response.status}`);
    }
    await Promise.all([mutate("/api/crawl/status"), mutate("/api/crawl/runs")]);
  };

  const startAutomation = async () => {
    setAutomationAction("start");
    try {
      const body = new FormData();
      body.append("interval_minutes", intervalInput);
      body.append("budget_usd", budgetInput);
      const response = await fetch("/api/crawl/scheduler/start", { method: "POST", body });
      if (!response.ok && response.status !== 202) {
        const payload = await response.json().catch(() => ({}));
        alert(payload.detail || payload.error ? `Automation failed: ${payload.detail || payload.error}` : `Automation failed: ${response.status}`);
      }
      await Promise.all([mutate("/api/crawl/status"), mutate("/api/crawl/runs")]);
    } finally {
      setAutomationAction(null);
    }
  };

  const stopAutomation = async () => {
    setAutomationAction("stop");
    try {
      const response = await fetch("/api/crawl/scheduler/stop", { method: "POST" });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        alert(payload.error ? `Pause failed: ${payload.error}` : `Pause failed: ${response.status}`);
      }
      await Promise.all([mutate("/api/crawl/status"), mutate("/api/crawl/runs")]);
    } finally {
      setAutomationAction(null);
    }
  };

  const updateCrawlerRuntime = async () => {
    setRuntimeAction(true);
    try {
      const response = await fetch("/api/crawl/runtime", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          parallelism: Number(parallelismInput),
          batch_size: Number(batchSizeInput),
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        alert(payload.error ? `Update failed: ${payload.error}` : `Update failed: ${response.status}`);
      }
      await mutate("/api/crawl/status");
    } finally {
      setRuntimeAction(false);
    }
  };

  const totalChanged = useMemo(
    () => (runs?.runs || []).reduce((sum, run) => sum + (run.repos_changed_count || 0), 0),
    [runs],
  );
  const recentRunsLabel = runs?.source === "extractions" ? "extraction runs" : "listed in Activity";
  const changedLabel = runs?.source === "extractions" ? "repos extracted" : "recent window";
  const schedulerRunning = Boolean(status?.scheduler?.running);
  const crawlerRunning = Boolean(status?.crawler?.running);
  const workRunning = Boolean(status?.lock?.held || crawlerRunning);
  const schedulerSource = status?.scheduler?.source || (schedulerRunning ? "external" : "stopped");
  const schedulerManaged = schedulerSource === "dashboard";
  const interval = formatInterval(schedulerInterval);
  const logPath = status?.scheduler?.log_path || status?.active_log_path;
  const logTail = status?.scheduler?.log_tail?.length ? status.scheduler.log_tail : (status?.active_log_tail || []);
  const pageDescription = workRunning
    ? "A crawl or re-index is running"
    : schedulerRunning
      ? `Automated crawl every ${interval}`
      : "Manual trigger only";

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Activity"
        description={pageDescription}
        actions={
          <>
            <Button variant="outline" onClick={() => void mutate(() => true)}>
              <RefreshCw size={14} /> Refresh
            </Button>
            <Button onClick={triggerNow} disabled={triggering || workRunning}>
              {triggering ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
              Trigger now
            </Button>
          </>
        }
      />
      <div className="grid min-h-0 grid-cols-1 xl:grid-cols-[minmax(0,1fr)_minmax(360px,28vw)]">
        <div className="min-w-0 overflow-auto p-4 sm:p-6 space-y-5">
          <div className="grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-3">
            <Card>
              <CardTitle>Scheduler</CardTitle>
              <CardValue>{schedulerRunning ? "On" : "Manual"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">
                {schedulerRunning ? `${schedulerManaged ? "managed" : "external"} · PID ${status?.scheduler?.pid} · ${status?.scheduler?.uptime || "running"}` : "Ready for manual trigger"}
              </div>
            </Card>
            <Card>
              <CardTitle>Current Work</CardTitle>
              <CardValue>{workRunning ? "Running" : "Idle"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">
                {status?.lock?.pid
                  ? `lock PID ${status.lock.pid}`
                  : status?.crawler?.pid
                    ? `crawler PID ${status.crawler.pid}`
                    : "Ready for trigger"}
              </div>
            </Card>
            <Card>
              <CardTitle>Recent Runs</CardTitle>
              <CardValue>{runs?.total ?? "-"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">{recentRunsLabel}</div>
            </Card>
            <Card>
              <CardTitle>Tick Budget</CardTitle>
              <CardValue>${schedulerBudget?.toFixed(0) ?? "-"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">per scheduled/manual run</div>
            </Card>
          </div>

          <section className="rounded-lg border border-border bg-bg-elevated overflow-hidden">
            <div className="grid grid-cols-1 lg:grid-cols-[190px_180px_minmax(0,1fr)] border-b border-border">
              <div className="border-b border-border px-4 py-3 lg:border-b-0 lg:border-r">
                <div className="text-xs uppercase tracking-wider text-fg-dim">Run Mode</div>
                <div className="mt-1 text-sm text-fg">
                  {schedulerRunning ? (schedulerManaged ? "Managed automation" : "External scheduler") : "Manual trigger only"}
                </div>
                <div className="mt-0.5 text-xs text-fg-dim">
                  {schedulerRunning ? "Runs continue until paused" : "Use Trigger now or enable automation"}
                </div>
              </div>
              <div className="border-b border-border px-4 py-3 lg:border-b-0 lg:border-r">
                <div className="text-xs uppercase tracking-wider text-fg-dim">Configured Interval</div>
                <div className="mt-1 text-sm text-fg">{interval}</div>
                <div className="mt-0.5 text-xs text-fg-dim">
                  {schedulerRunning ? "Next ticks use this cadence" : "Only applies after automation starts"}
                </div>
              </div>
              <div className="px-4 py-3">
                <div className="text-xs uppercase tracking-wider text-fg-dim">Workspace</div>
                <div className="mt-1 truncate font-mono text-xs text-fg-muted">{status?.workspace_root || "-"}</div>
                <div className="mt-0.5 truncate font-mono text-xs text-fg-dim">{status?.workspace_config || "-"}</div>
              </div>
            </div>
            <div className="grid grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_auto] gap-4 border-b border-border px-4 py-3">
              <div>
                <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-fg-dim">
                  <Settings2 size={13} /> Automation
                </div>
                <div className="mt-1 text-sm text-fg-muted">
                  {schedulerRunning
                    ? schedulerManaged
                      ? `Enabled every ${interval}. Update values to restart the scheduler with a new cadence.`
                      : "A scheduler is running outside the dashboard. Stop that process before switching to managed control."
                    : "Enable scheduled crawls here, or keep using manual trigger for one-off runs."}
                </div>
              </div>
              <div className="flex flex-wrap items-end gap-2 2xl:justify-end">
                <label className="block w-[120px]">
                  <span className="mb-1 block text-xs text-fg-dim">Interval (min)</span>
                  <Input
                    id="scheduler-interval"
                    type="number"
                    min={1}
                    max={10080}
                    value={intervalInput}
                    disabled={schedulerSource === "external" || automationAction !== null}
                    onChange={(event) => setIntervalInput(event.target.value)}
                  />
                </label>
                <label className="block w-[120px]">
                  <span className="mb-1 block text-xs text-fg-dim">Budget ($)</span>
                  <Input
                    id="scheduler-budget"
                    type="number"
                    min={0.01}
                    step={0.01}
                    value={budgetInput}
                    disabled={schedulerSource === "external" || automationAction !== null}
                    onChange={(event) => setBudgetInput(event.target.value)}
                  />
                </label>
                <Button
                  onClick={startAutomation}
                  disabled={schedulerSource === "external" || automationAction !== null}
                  className="h-9"
                >
                  {automationAction === "start" ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                  {schedulerRunning ? "Update" : "Enable"}
                </Button>
                <Button
                  variant="outline"
                  onClick={stopAutomation}
                  disabled={!schedulerManaged || automationAction !== null}
                  className="h-9"
                >
                  {automationAction === "stop" ? <Loader2 size={14} className="animate-spin" /> : <Pause size={14} />}
                  Pause
                </Button>
              </div>
            </div>
            <div className="grid grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_auto] gap-4 border-b border-border px-4 py-3">
              <div>
                <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-fg-dim">
                  <Settings2 size={13} /> Crawler limits
                </div>
                <div className="mt-1 text-sm text-fg-muted">
                  Applies before the next batch. Current source: {status?.crawler_runtime?.source || "default"}.
                </div>
              </div>
              <div className="flex flex-wrap items-end gap-2 2xl:justify-end">
                <label className="block w-[120px]">
                  <span className="mb-1 block text-xs text-fg-dim">Parallelism</span>
                  <Input
                    id="crawler-parallelism"
                    type="number"
                    min={1}
                    max={64}
                    value={parallelismInput}
                    disabled={runtimeAction}
                    onChange={(event) => setParallelismInput(event.target.value)}
                  />
                </label>
                <label className="block w-[120px]">
                  <span className="mb-1 block text-xs text-fg-dim">Batch size</span>
                  <Input
                    id="crawler-batch-size"
                    type="number"
                    min={1}
                    max={500}
                    value={batchSizeInput}
                    disabled={runtimeAction}
                    onChange={(event) => setBatchSizeInput(event.target.value)}
                  />
                </label>
                <Button
                  variant="outline"
                  onClick={updateCrawlerRuntime}
                  disabled={runtimeAction}
                  className="h-9"
                >
                  {runtimeAction ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
                  Apply
                </Button>
              </div>
            </div>
            <div className="px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-xs uppercase tracking-wider text-fg-dim">Live Output</div>
                  <div className="text-xs text-fg-dim">{logPath || "No scheduler or trigger log yet"}</div>
                </div>
                {status?.crawler?.running && (
                  <div className="rounded border border-accent/30 bg-accent/10 px-2 py-1 text-xs text-fg">
                    crawler PID {status.crawler.pid}
                  </div>
                )}
              </div>
              {logTail.length > 0 ? (
                <pre className="mt-3 max-h-40 overflow-auto rounded border border-border bg-bg p-3 text-xs text-fg-muted whitespace-pre-wrap">
                  {logTail.join("\n")}
                </pre>
              ) : (
                <div className="mt-3 rounded border border-border bg-bg px-3 py-2 text-sm text-fg-muted">
                  Trigger a crawl to see scheduler and crawler events here.
                </div>
              )}
            </div>
          </section>

          <div className="text-xs text-fg-dim">
            Recent run table: {totalChanged} {changedLabel}. Select a row to inspect events, cost, changed repos, and re-index actions.
          </div>

          <section className="rounded-lg border border-border bg-bg-elevated overflow-hidden">
            <div className="overflow-x-auto">
              <div className="min-w-[780px]">
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
              </div>
            </div>
          </section>
        </div>

        <aside className="min-w-0 overflow-hidden border-t border-border bg-bg-elevated xl:border-l xl:border-t-0">
          {!detail && <div className="p-6 text-sm text-fg-muted">Select a run.</div>}
          {detail && <RunDetail detail={detail} onReindexRepo={triggerRepo} />}
        </aside>
      </div>
    </div>
  );
}

function RunDetail({ detail, onReindexRepo }: { detail: CrawlRunDetail; onReindexRepo: (repo: string) => void }) {
  const changedRepos = detail.repos_changed || [];
  const visibleChangedRepos = changedRepos.slice(-30).reverse();
  const events = detail.events || [];
  const visibleEvents = events.slice(-80).reverse();

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_minmax(0,1fr)] gap-5 p-6 text-sm">
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

      <div className="min-h-0 overflow-auto pr-1 space-y-5">
        {changedRepos.length > 0 && (
          <section>
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="text-xs uppercase tracking-wider text-fg-dim">Changed repos</h3>
              {changedRepos.length > visibleChangedRepos.length && (
                <span className="text-xs text-fg-dim">Latest {visibleChangedRepos.length} of {changedRepos.length}</span>
              )}
            </div>
            <div className="max-h-[42vh] space-y-1 overflow-auto pr-1">
              {visibleChangedRepos.map((repo, index) => (
                <div key={`${repo.repo}-${repo.reason || "changed"}-${index}`} className="rounded border border-border bg-bg px-3 py-2">
                  <div className="flex items-center gap-2 text-fg">
                    <GitBranch size={13} className="text-fg-dim" />
                    <span className="font-mono text-xs truncate">{repo.repo}</span>
                    <button
                      type="button"
                      onClick={() => onReindexRepo(repo.repo)}
                      className="ml-auto inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] text-fg-muted hover:text-fg"
                    >
                      <RotateCw size={11} /> Re-index
                    </button>
                  </div>
                  <div className="mt-1 text-xs text-fg-dim">{repo.reason || "changed"}</div>
                </div>
              ))}
            </div>
          </section>
        )}

        {events.length > 0 && (
          <section>
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 className="text-xs uppercase tracking-wider text-fg-dim">Timeline</h3>
              {events.length > visibleEvents.length && (
                <span className="text-xs text-fg-dim">Latest {visibleEvents.length} of {events.length}</span>
              )}
            </div>
            <ol className="max-h-80 space-y-2 overflow-auto pr-1">
              {visibleEvents.map((event, index) => (
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
