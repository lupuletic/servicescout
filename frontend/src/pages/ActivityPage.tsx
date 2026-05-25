import { useMemo, useState, type ReactNode } from "react";
import useSWR, { useSWRConfig } from "swr";
import { AlertCircle, CheckCircle2, Clock3, GitBranch, Loader2, Pause, Play, RefreshCw, RotateCw, Settings2, TerminalSquare, XCircle } from "lucide-react";
import { type CrawlRunDetail, type CrawlRunsPayload, type CrawlStatusPayload } from "@/lib/api";
import { Button, Card, CardTitle, CardValue, Input, PageHeader, StatusBadge } from "@/components/ui";
import { cn } from "@/lib/cn";

type ChangedRepo = NonNullable<CrawlRunDetail["repos_changed"]>[number];
type CompletionItem = NonNullable<CrawlRunDetail["recent_completions"]>[number];
type ActiveWorker = {
  repo: string;
  workerId?: string;
  pid?: number;
  elapsedSeconds?: number;
  phase?: string;
  startedAt?: string | null;
  lastEvent?: string;
  lastMessage?: string;
  lastAt?: string | null;
};
type FailureItem = {
  repo: string;
  category: string;
  status: string;
  detail?: string;
  event?: string;
};

function StatusGlyph({ status, className }: { status?: string; className?: string }) {
  if (status === "running") return <Loader2 size={14} className={cn("animate-spin", className)} />;
  if (status === "ok" || status === "no_changes") return <CheckCircle2 size={14} className={className} />;
  if (status === "crawler_failed" || status === "exception") return <XCircle size={14} className={className} />;
  if (status === "tick_skipped_busy" || status === "abandoned") return <Clock3 size={14} className={className} />;
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
  return formatSeconds(seconds);
}

function runDuration(run?: Pick<CrawlRunDetail, "started_at" | "finished_at" | "duration_seconds"> | null) {
  if (!run) return "-";
  if (run.duration_seconds != null) return formatSeconds(run.duration_seconds);
  return duration(run.started_at, run.finished_at);
}

function formatSeconds(seconds?: number | null) {
  if (seconds == null) return "-";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function fmtMoney(value?: number | null) {
  if (value == null) return "-";
  return `$${value.toFixed(value >= 10 ? 0 : 2)}`;
}

function numeric(value: unknown) {
  const parsed = typeof value === "number" ? value : typeof value === "string" ? Number(value) : Number.NaN;
  return Number.isFinite(parsed) ? parsed : null;
}

function formatInterval(minutes?: number) {
  if (!minutes) return "-";
  if (minutes < 60) return `${minutes}m`;
  const hours = minutes / 60;
  return Number.isInteger(hours) ? `${minutes}m (${hours}h)` : `${minutes}m`;
}

function parseJson(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "string") return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function parseLogEvents(lines?: string[]) {
  return (lines || []).map(parseJson).filter((event): event is Record<string, unknown> => Boolean(event));
}

function shortRepo(value?: unknown) {
  const text = String(value || "");
  return text.includes("/") ? text.split("/").pop() || text : text;
}

function workerLabel(worker: ActiveWorker) {
  return worker.lastMessage || worker.phase || worker.lastEvent || "running";
}

function eventTimestamp(event: Record<string, unknown>) {
  return typeof event.ts === "string" ? event.ts : null;
}

function formatEvent(event: Record<string, unknown>) {
  const name = String(event.event || "event");
  const repo = typeof event.repo === "string" ? event.repo : "";
  if (name === "tick_start") {
    return `Run started: ${event.run_id || "pending"}`;
  }
  if (name === "manual_reindex_selected") {
    return `Manual re-index selected: ${event.count || "-"} repos`;
  }
  if (name === "crawler_invoke") {
    const repos = Array.isArray(event.repos) ? event.repos.length : "-";
    return `Crawler invoked: ${repos} repos`;
  }
  if (name === "repo_clone_failed" || name === "seed_clone_failed") {
    const failure = classifyFailure({
      repo: repo || event.seed,
      status: "clone_failed",
      error: event.error,
      event: name,
    });
    return `${shortRepo(failure.repo)} ${failure.category.toLowerCase()}`;
  }
  if (name === "batch_start") {
    return `Batch started: ${event.n || "-"} repos, ${event.parallelism || "-"} workers, ${event.stale_remaining || "-"} waiting`;
  }
  if (name === "runtime_config_applied") {
    return `Runtime limits applied: ${event.parallelism || "-"} workers, batch ${event.batch_size || "-"}`;
  }
  if (name === "tag_reconcile_start") {
    return `Tag reconciliation started${event.llm_assist ? " with LLM assist" : ""}`;
  }
  if (name === "tag_reconcile_done") {
    if (event.skipped) return `Tag reconciliation skipped: ${event.reason || "unchanged"}`;
    return `Tag reconciliation finished${event.returncode === 0 ? "" : ` with exit ${event.returncode}`}`;
  }
  if (name === "extractor_process_start") {
    return `${shortRepo(repo)} started`;
  }
  if (name === "extractor_process_exit") {
    const durationSeconds = numeric(event.duration_seconds);
    const durationText = durationSeconds == null ? "" : ` in ${formatSeconds(durationSeconds)}`;
    return `${shortRepo(repo)} finished ${event.status || "unknown"}${durationText}`;
  }
  if (name === "repo_done") {
    const durationSeconds = numeric(event.duration_seconds);
    const cost = numeric(event.cost_usd);
    const suffix = [
      durationSeconds == null ? null : formatSeconds(durationSeconds),
      cost == null ? null : fmtMoney(cost),
    ].filter(Boolean).join(" · ");
    return `${shortRepo(repo)} ${event.status || "done"}${suffix ? ` · ${suffix}` : ""}`;
  }
  if (name === "extractor_heartbeat") {
    const elapsedSeconds = numeric(event.elapsed_seconds);
    return `${shortRepo(repo)} running${elapsedSeconds == null ? "" : ` for ${formatSeconds(elapsedSeconds)}`}`;
  }
  if (name === "extractor_child_event") {
    const inner = parseJson(event.line);
    if (inner?.event === "turn_completed") {
      return `${shortRepo(repo)} agent turn completed`;
    }
    if (inner?.event === "item_completed" || inner?.event === "item_started") {
      return `${shortRepo(repo)} ${String(inner.item_type || "item").replace(/_/g, " ")} ${inner.status || ""}`.trim();
    }
    return `${shortRepo(repo)} agent event`;
  }
  return repo ? `${shortRepo(repo)} ${name.replace(/_/g, " ")}` : name.replace(/_/g, " ");
}

function formatLogLine(line: string) {
  const parsed = parseJson(line);
  return parsed ? formatEvent(parsed) : line;
}

function repoEventMetadata(events: Array<Record<string, unknown>>) {
  const metadata = new Map<string, Partial<ChangedRepo>>();
  for (const event of events) {
    const name = String(event.event || "");
    if (name !== "extractor_process_exit" && name !== "repo_done") continue;
    const repo = typeof event.repo === "string" ? event.repo : "";
    if (!repo) continue;
    const existing = metadata.get(repo) || {};
    const durationSeconds = numeric(event.duration_seconds);
    const costUsd = numeric(event.cost_usd);
    metadata.set(repo, {
      ...existing,
      status: typeof event.status === "string" ? event.status : existing.status,
      duration_seconds: durationSeconds ?? existing.duration_seconds,
      cost_usd: costUsd ?? existing.cost_usd,
      error: typeof event.error === "string" ? event.error : existing.error,
    });
  }
  return metadata;
}

function isFailureStatus(status?: string) {
  if (!status) return false;
  return !["ok", "completed", "running", "in_progress", "started", "queued"].includes(status);
}

function classifyFailure(input: { repo?: unknown; status?: unknown; error?: unknown; event?: unknown }): FailureItem {
  const repo = String(input.repo || "unknown");
  const status = String(input.status || input.event || "failed");
  const detail = typeof input.error === "string" ? input.error.trim().replace(/\s+/g, " ").slice(0, 220) : "";
  const text = `${status}\n${detail}`.toLowerCase();
  let category = "Extractor error";
  if (text.includes("saml") || text.includes("sso") || text.includes("403") || text.includes("not authorized") || text.includes("permission")) {
    category = "Permission / SSO";
  } else if (text.includes("not found") || text.includes("404") || text.includes("repository not found")) {
    category = "Not found";
  } else if (text.includes("could not read username") || text.includes("authentication failed") || text.includes("authenticate")) {
    category = "Auth required";
  } else if (status.includes("clone") || text.includes("git clone") || text.includes("unable to access")) {
    category = "Clone failed";
  }
  return { repo, status, detail, category, event: String(input.event || "") };
}

function failureItems(repos: ChangedRepo[], events: Array<Record<string, unknown>>) {
  const failures: FailureItem[] = [];
  for (const repo of repos) {
    if (!isFailureStatus(repo.status)) continue;
    failures.push(classifyFailure({
      repo: repo.repo,
      status: repo.status,
      error: repo.error,
      event: repo.reason,
    }));
  }
  for (const event of events) {
    const name = String(event.event || "");
    if (
      !["repo_clone_failed", "seed_clone_failed", "gh_repo_api_error", "gh_repo_list_error"].includes(name)
      && !(["repo_done", "extractor_process_exit"].includes(name) && isFailureStatus(String(event.status || "")))
    ) continue;
    failures.push(classifyFailure({
      repo: event.repo || event.seed || event.org,
      status: name,
      error: event.error,
      event: name,
    }));
  }
  return failures;
}

function withRepoMetadata(repos: ChangedRepo[], events: Array<Record<string, unknown>>) {
  const metadata = repoEventMetadata(events);
  return repos.map((repo) => ({ ...metadata.get(repo.repo), ...repo }));
}

function summariseRun(detail?: CrawlRunDetail) {
  const events = detail?.events || [];
  const repos = withRepoMetadata(detail?.repos_changed || [], events);
  const active = new Map<string, ActiveWorker>();
  let latestBatch: Record<string, unknown> | null = detail?.latest_batch || null;
  for (const event of events) {
    const name = String(event.event || "");
    const repo = typeof event.repo === "string" ? event.repo : "";
    if (name === "batch_start") latestBatch = event;
    if (name === "extractor_process_start" && repo) {
      active.set(repo, {
        repo,
        pid: numeric(event.pid) ?? undefined,
        phase: "extracting",
        lastEvent: "started",
        lastMessage: "extractor starting",
        lastAt: eventTimestamp(event),
        startedAt: eventTimestamp(event),
      });
    }
    if (name === "extractor_heartbeat" && repo) {
      const existing = active.get(repo);
      active.set(repo, {
        repo,
        pid: numeric(event.pid) ?? existing?.pid,
        elapsedSeconds: numeric(event.elapsed_seconds) ?? existing?.elapsedSeconds,
        phase: "extracting",
        lastEvent: "heartbeat",
        lastMessage: "heartbeat",
        lastAt: eventTimestamp(event),
        startedAt: existing?.startedAt,
      });
    }
    if (name === "extractor_child_event" && repo) {
      const existing = active.get(repo);
      if (existing) {
        const inner = parseJson(event.line);
        active.set(repo, {
          ...existing,
          phase: String(inner?.item_type || inner?.event || existing.phase || "").replace(/_/g, " "),
          lastEvent: formatEvent(event),
          lastMessage: formatEvent(event),
          lastAt: eventTimestamp(event),
        });
      }
    }
    if ((name === "extractor_process_exit" || name === "repo_done") && repo) active.delete(repo);
  }
  const failures = failureItems(repos, events).length;
  const recentCompletions = detail?.recent_completions || [];
  const ok = Math.max(
    repos.filter((repo) => repo.status === "ok").length,
    recentCompletions.filter((repo) => repo.status === "ok").length,
  );
  const totalDuration = repos.reduce((sum, repo) => sum + (repo.duration_seconds || 0), 0);
  const activeWorkers = detail?.active_workers?.length
    ? detail.active_workers.map((worker) => ({
      repo: worker.repo,
      workerId: worker.worker_id,
      pid: worker.pid,
      elapsedSeconds: worker.elapsed_seconds,
      phase: worker.phase,
      startedAt: worker.started_at,
      lastAt: worker.last_at,
      lastEvent: worker.last_event,
      lastMessage: worker.last_message,
    }))
    : [...active.values()].slice(-12).reverse();
  const completed = detail?.repos_completed_count ?? (recentCompletions.length || repos.length || detail?.repos_changed_count || 0);
  return {
    checked: detail?.repos_checked,
    completed,
    ok,
    failures,
    activeWorkers,
    activeRepos: activeWorkers.map((worker) => worker.repo),
    activeCount: detail?.active_workers?.length ?? active.size,
    latestBatch,
    avgDurationSeconds: repos.length ? totalDuration / repos.length : null,
  };
}

function activeRunFromStatus(status?: CrawlStatusPayload): CrawlRunDetail | null {
  if (!status?.lock?.held) return null;
  if (status.active_run) return status.active_run;
  const events = parseLogEvents(status.active_log_tail);
  const tickStart = [...events].reverse().find((event) => event.event === "tick_start");
  const selected = [...events].reverse().find((event) => event.event === "manual_reindex_selected" || event.event === "change_detected");
  const invoked = [...events].reverse().find((event) => event.event === "crawler_invoke");
  const runId = String(tickStart?.run_id || selected?.run_id || invoked?.run_id || `active-${status.lock.pid || "crawl"}`);
  const selectedCount = numeric(selected?.count);
  const invokedRepos = Array.isArray(invoked?.repos) ? invoked.repos.filter((repo) => typeof repo === "string") as string[] : [];
  const completed = events.filter((event) => event.event === "repo_done").length;
  const startedAt = eventTimestamp(tickStart || {}) || status.lock.acquired_at;
  const observedAt = new Date().toISOString();
  const started = startedAt ? new Date(startedAt).getTime() : Number.NaN;
  const durationSeconds = Number.isFinite(started) ? Math.max((Date.now() - started) / 1000, 0) : undefined;
  return {
    run_id: runId,
    trigger: selected?.event === "manual_reindex_selected" ? "manual-reindex" : "manual",
    started_at: startedAt,
    observed_at: observedAt,
    status: "running",
    repos_checked: selectedCount ?? (invokedRepos.length || undefined),
    repos_changed_count: completed,
    budget_usd: status.scheduler?.budget_usd ?? status.budget_usd,
    duration_seconds: durationSeconds == null ? undefined : Math.round(durationSeconds),
    workspace_root: status.workspace_root,
    repos_changed: [],
    events,
  };
}

export function ActivityPage() {
  const { data: status } = useSWR<CrawlStatusPayload>("/api/crawl/status", { refreshInterval: 3000 });
  const { data: runs, isLoading } = useSWR<CrawlRunsPayload>("/api/crawl/runs", { refreshInterval: 5000 });
  const { mutate } = useSWRConfig();
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [triggering, setTriggering] = useState(false);
  const [automationAction, setAutomationAction] = useState<"start" | "stop" | null>(null);
  const [runtimeAction, setRuntimeAction] = useState(false);
  const [intervalInput, setIntervalInput] = useState<string | null>(null);
  const [budgetInput, setBudgetInput] = useState<string | null>(null);
  const [parallelismInput, setParallelismInput] = useState<string | null>(null);
  const [batchSizeInput, setBatchSizeInput] = useState<string | null>(null);
  const activeRun = useMemo(() => activeRunFromStatus(status), [status]);
  const displayedRuns = useMemo(() => {
    const rows = runs?.runs || [];
    if (!activeRun) return rows;
    return [activeRun, ...rows.filter((run) => run.run_id !== activeRun.run_id)];
  }, [activeRun, runs]);
  const selected = selectedRun || displayedRuns[0]?.run_id || null;
  const selectedIsActive = Boolean(activeRun && selected === activeRun.run_id);
  const { data: detail } = useSWR<CrawlRunDetail>(selected && !selectedIsActive ? `/api/crawl/runs/${selected}` : null, { refreshInterval: 3000 });
  const selectedDetail = selectedIsActive ? activeRun : detail;
  const schedulerInterval = status?.scheduler?.interval_minutes ?? status?.interval_minutes;
  const schedulerBudget = status?.scheduler?.budget_usd ?? status?.budget_usd;
  const intervalValue = intervalInput ?? String(schedulerInterval ?? 360);
  const budgetValue = budgetInput ?? String(schedulerBudget ?? 20);
  const parallelismValue = parallelismInput ?? String(status?.crawler_runtime?.parallelism ?? 8);
  const batchSizeValue = batchSizeInput ?? String(status?.crawler_runtime?.batch_size ?? 24);

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
      body.append("interval_minutes", intervalValue);
      body.append("budget_usd", budgetValue);
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
          parallelism: Number(parallelismValue),
          batch_size: Number(batchSizeValue),
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
    () => displayedRuns.reduce((sum, run) => sum + (run.repos_changed_count || 0), 0),
    [displayedRuns],
  );
  const changedLabel = runs?.source === "extractions" ? "repos extracted" : "recent window";
  const schedulerRunning = Boolean(status?.scheduler?.running);
  const crawlerRunning = Boolean(status?.crawler?.running);
  const workRunning = Boolean(status?.lock?.held || crawlerRunning);
  const schedulerSource = status?.scheduler?.source || (schedulerRunning ? "external" : "stopped");
  const schedulerManaged = schedulerSource === "dashboard";
  const interval = formatInterval(schedulerInterval);
  const logPath = status?.scheduler?.log_path || status?.active_log_path;
  const logTail = status?.scheduler?.log_tail?.length ? status.scheduler.log_tail : (status?.active_log_tail || []);
  const runStats = useMemo(() => summariseRun(selectedDetail || undefined), [selectedDetail]);
  const runChecked = runStats.checked ?? (selectedDetail ? undefined : status?.last_run?.repos_checked);
  const runCompleted = selectedDetail ? runStats.completed : status?.last_run?.repos_changed_count || 0;
  const runProgress = runChecked ? Math.min(100, Math.round((runCompleted / runChecked) * 100)) : null;
  const runBudget = selectedDetail?.budget_usd ?? status?.last_run?.budget_usd ?? schedulerBudget;
  const runCost = selectedDetail?.cost_usd ?? status?.last_run?.cost_usd;
  const runSpendSoFar = numeric(runStats.latestBatch?.run_spent_so_far);
  const catalogSpend = numeric(runStats.latestBatch?.spent_so_far) ?? selectedDetail?.catalog_cost_usd ?? status?.last_run?.catalog_cost_usd;
  const spendValue = runCost ?? runSpendSoFar ?? catalogSpend;
  const spendHint = runCost != null
    ? `final run spend${runBudget != null ? ` of $${runBudget}` : ""}`
    : runSpendSoFar != null
      ? `this run so far${runBudget != null ? ` of $${runBudget}` : ""}`
      : catalogSpend != null
        ? `${fmtMoney(catalogSpend)} catalog spend`
        : "cost appears after extraction";
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
              <CardTitle>Status</CardTitle>
              <CardValue>{workRunning ? "Running" : schedulerRunning ? "Scheduled" : "Idle"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">
                {status?.crawler?.pid ? `crawler PID ${status.crawler.pid}` : schedulerRunning ? `${schedulerManaged ? "managed" : "external"} scheduler` : "Ready for trigger"}
              </div>
            </Card>
            <Card>
              <CardTitle>Progress</CardTitle>
              <CardValue>{runChecked ? `${runCompleted}/${runChecked}` : runCompleted || "-"}</CardValue>
              <div className="text-xs text-fg-dim mt-1">
                {runProgress != null ? `${runProgress}% of checked repos` : "Waiting for run detail"}
              </div>
            </Card>
            <Card>
              <CardTitle>Active</CardTitle>
              <CardValue>{runStats.activeCount || (crawlerRunning ? "..." : 0)}</CardValue>
              <div className="text-xs text-fg-dim mt-1">
                {status?.crawler_runtime ? `${status.crawler_runtime.parallelism} worker limit · batch ${status.crawler_runtime.batch_size}` : "worker pool"}
              </div>
            </Card>
            <Card>
              <CardTitle>Spend</CardTitle>
              <CardValue>{fmtMoney(spendValue)}</CardValue>
              <div className="text-xs text-fg-dim mt-1">{spendHint}</div>
            </Card>
            <Card>
              <CardTitle>Failures</CardTitle>
              <CardValue>{runStats.failures}</CardValue>
              <div className="text-xs text-fg-dim mt-1">
                {runStats.ok ? `${runStats.ok} ok` : "No completed successes yet"}
              </div>
            </Card>
          </div>
          <div className="rounded-lg border border-border bg-bg-elevated px-4 py-3">
            <div className="flex items-center justify-between gap-3 text-xs uppercase tracking-wider text-fg-dim">
              <span>Run progress</span>
              <span>{runProgress != null ? `${runProgress}%` : "waiting for run detail"}</span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded bg-bg">
              <div
                className="h-full rounded bg-accent transition-all"
                style={{ width: `${runProgress ?? 0}%` }}
              />
            </div>
          </div>

          <section className="rounded-lg border border-border bg-bg-elevated overflow-hidden">
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_260px] gap-4 border-b border-border px-4 py-3">
              <div className="min-w-0">
                <div className="text-xs uppercase tracking-wider text-fg-dim">Workspace</div>
                <div className="mt-1 truncate font-mono text-xs text-fg-muted">{status?.workspace_root || "-"}</div>
                <div className="mt-0.5 truncate font-mono text-xs text-fg-dim">{status?.workspace_config || "-"}</div>
              </div>
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <div className="text-xs uppercase tracking-wider text-fg-dim">Mode</div>
                  <div className="mt-1 text-fg">{schedulerRunning ? (schedulerManaged ? "Scheduled" : "External") : "Manual"}</div>
                </div>
                <div>
                  <div className="text-xs uppercase tracking-wider text-fg-dim">Interval</div>
                  <div className="mt-1 text-fg">{interval}</div>
                </div>
              </div>
            </div>
            <div className="grid grid-cols-1 2xl:grid-cols-2 border-b border-border">
              <div className="border-b border-border px-4 py-3 2xl:border-b-0 2xl:border-r">
                <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-fg-dim">
                  <Settings2 size={13} /> Automation
                </div>
                <div className="mt-1 min-h-8 text-sm text-fg-muted">
                  {schedulerRunning
                    ? schedulerManaged
                      ? `Enabled every ${interval}. Update values to restart the scheduler with a new cadence.`
                      : "A scheduler is running outside the dashboard. Stop that process before switching to managed control."
                    : "Enable scheduled crawls here, or keep using manual trigger for one-off runs."}
                </div>
                <div className="mt-3 flex flex-wrap items-end gap-2">
                  <label className="block w-[120px]">
                    <span className="mb-1 block text-xs text-fg-dim">Interval (min)</span>
                    <Input
                      id="scheduler-interval"
                      type="number"
                      min={1}
                      max={10080}
                      value={intervalValue}
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
                      value={budgetValue}
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
              <div className="px-4 py-3">
                <div className="flex items-center gap-2 text-xs uppercase tracking-wider text-fg-dim">
                  <Settings2 size={13} /> Crawler limits
                </div>
                <div className="mt-1 min-h-8 text-sm text-fg-muted">
                  Applies before the next batch. Current source: {status?.crawler_runtime?.source || "default"}.
                </div>
                <div className="mt-3 flex flex-wrap items-end gap-2">
                  <label className="block w-[120px]">
                    <span className="mb-1 block text-xs text-fg-dim">Parallelism</span>
                    <Input
                      id="crawler-parallelism"
                      type="number"
                      min={1}
                      max={64}
                      value={parallelismValue}
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
                      value={batchSizeValue}
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
            </div>
            <div className="px-4 py-3">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-xs uppercase tracking-wider text-fg-dim">Worker snapshot</div>
                  <div className="text-xs text-fg-dim">
                    {runStats.activeCount
                      ? `${runStats.activeCount} active · ${status?.crawler_runtime?.parallelism || "-"} worker limit`
                      : logPath || "No active workers yet"}
                  </div>
                </div>
                {status?.crawler?.running && (
                  <div className="rounded border border-accent/30 bg-accent/10 px-2 py-1 text-xs text-fg">
                    crawler PID {status.crawler.pid}
                  </div>
                )}
              </div>
              {runStats.activeWorkers.length > 0 ? (
                <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                  {runStats.activeWorkers.slice(0, 12).map((worker) => (
                    <div key={worker.repo} className="min-w-0 rounded border border-border bg-bg px-3 py-2">
                      <div className="flex items-center gap-2">
                        <span className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-fg-dim">
                          {worker.workerId || "W"}
                        </span>
                        <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg">{worker.repo}</span>
                      </div>
                      <div className="mt-1 truncate text-xs text-fg-dim">{workerLabel(worker)}</div>
                    </div>
                  ))}
                </div>
              ) : logTail.length > 0 ? (
                <div className="mt-3 rounded border border-border bg-bg">
                  {logTail.slice(-3).reverse().map((line, index) => (
                    <div key={index} className="border-b border-border px-3 py-2 last:border-b-0">
                      <div className="truncate text-sm text-fg-muted">{formatLogLine(line)}</div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="mt-3 rounded border border-border bg-bg px-3 py-2 text-sm text-fg-muted">
                  Trigger a crawl to see scheduler and crawler events here.
                </div>
              )}
            </div>
          </section>

          <div className="text-xs text-fg-dim">
            Recent run table: {totalChanged} {changedLabel}. Select a row to inspect progress, failures, recent completions, and live events.
          </div>

          <section className="rounded-lg border border-border bg-bg-elevated overflow-hidden">
            <div className="overflow-x-auto">
              <div className="min-w-[780px]">
                <div className="grid grid-cols-[140px_minmax(220px,1fr)_100px_80px_90px_85px] gap-3 px-4 py-2 text-xs uppercase tracking-wider text-fg-dim border-b border-border">
                  <div>Started</div>
                  <div>Run</div>
                  <div>Status</div>
                  <div>Done</div>
                  <div className="text-right">Cost</div>
                  <div className="text-right">Duration</div>
                </div>
                {isLoading && <div className="p-6 text-fg-muted">Loading runs...</div>}
                {!isLoading && displayedRuns.length === 0 && (
                  <div className="p-6 text-fg-muted">No activity recorded.</div>
                )}
                {displayedRuns.map((run) => {
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
                      <div className="text-fg-muted">{run.repos_completed_count ?? run.repos_changed_count}</div>
                      <div className="text-right text-fg-muted tabular-nums">{fmtMoney(run.cost_usd)}</div>
                      <div className="text-right text-fg-muted">{runDuration(run)}</div>
                    </button>
                  );
                })}
              </div>
            </div>
          </section>
        </div>

        <aside className="min-w-0 overflow-hidden border-t border-border bg-bg-elevated xl:border-l xl:border-t-0">
          {!selectedDetail && <div className="p-6 text-sm text-fg-muted">Select a run.</div>}
          {selectedDetail && <RunDetail detail={selectedDetail} onReindexRepo={triggerRepo} />}
        </aside>
      </div>
    </div>
  );
}

function RunDetail({ detail, onReindexRepo }: { detail: CrawlRunDetail; onReindexRepo: (repo: string) => void }) {
  const [tab, setTab] = useState<"overview" | "workers" | "events" | "failures">("overview");
  const [selectedWorkerRepo, setSelectedWorkerRepo] = useState<string | null>(null);
  const events = detail.events || [];
  const changedRepos = withRepoMetadata(detail.repos_changed || [], events);
  const stats = summariseRun(detail);
  const failures = failureItems(changedRepos, events);
  const completions: CompletionItem[] = detail.recent_completions?.length
    ? detail.recent_completions
    : changedRepos.slice(-20).reverse().map((repo) => ({
      repo: repo.repo,
      status: repo.status,
      duration_seconds: repo.duration_seconds,
      cost_usd: repo.cost_usd,
      reason: repo.reason,
      error: repo.error,
    }));
  const currentRunSpend = detail.cost_usd ?? numeric(stats.latestBatch?.run_spent_so_far);
  const batchCatalogSpend = numeric(stats.latestBatch?.spent_so_far);
  const observedFinish = detail.finished_at || detail.observed_at;
  const eventCount = detail.event_count ?? events.length;
  const filteredEvents = selectedWorkerRepo
    ? events.filter((event) => event.repo === selectedWorkerRepo)
    : events;
  const visibleEvents = filteredEvents.slice(-80).reverse();
  const progress = stats.checked ? Math.min(100, Math.round((stats.completed / stats.checked) * 100)) : null;

  return (
    <div className="grid h-full min-h-0 grid-rows-[auto_auto_auto_minmax(0,1fr)] gap-4 p-6 text-sm">
      <div>
        <div className="flex items-center gap-2">
          <StatusGlyph status={detail.status} className="text-fg-muted" />
          <StatusBadge status={detail.status || "unknown"} />
        </div>
        <h2 className="mt-2 font-mono text-sm text-fg break-all">{detail.run_id}</h2>
        <div className="mt-1 text-xs text-fg-dim">
          {formatDate(detail.started_at)} - {detail.status === "running" && !detail.finished_at ? "now" : formatDate(observedFinish)}
        </div>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <MiniMetric label="Selected" value={detail.repos_checked ?? "-"} />
        <MiniMetric label="Completed" value={stats.completed || "-"} />
        <MiniMetric label="Running" value={stats.activeCount} />
        <MiniMetric label="Duration" value={runDuration(detail)} />
        <MiniMetric label="Spend" value={fmtMoney(currentRunSpend)} />
        <MiniMetric label="Failures" value={stats.failures} />
      </div>

      <div className="space-y-3">
        <div className="h-1.5 overflow-hidden rounded bg-bg">
          <div className="h-full rounded bg-accent transition-all" style={{ width: `${progress ?? 0}%` }} />
        </div>
        <div className="grid grid-cols-4 gap-1 rounded border border-border bg-bg p-1">
          {[
            ["overview", "Overview"],
            ["workers", "Workers"],
            ["events", "Events"],
            ["failures", "Failures"],
          ].map(([value, label]) => (
            <button
              key={value}
              type="button"
              onClick={() => setTab(value as "overview" | "workers" | "events" | "failures")}
              className={cn(
                "rounded px-2 py-1.5 text-xs text-fg-muted hover:text-fg",
                tab === value && "bg-accent/20 text-fg",
              )}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="min-h-0 overflow-auto pr-1 space-y-5">
        {tab === "overview" && (
          <>
            <BatchSummary
              latestBatch={stats.latestBatch}
              currentRunSpend={currentRunSpend}
              batchCatalogSpend={batchCatalogSpend}
              budgetUsd={detail.budget_usd}
            />
            <WorkerList
              workers={stats.activeWorkers}
              selectedRepo={selectedWorkerRepo}
              onSelect={(repo) => {
                setSelectedWorkerRepo(repo);
                setTab("events");
              }}
              compact
            />
            <CompletionList completions={completions.slice(0, 8)} onReindexRepo={onReindexRepo} compact />
            <FailureList failures={failures.slice(0, 6)} />
          </>
        )}

        {tab === "workers" && (
          <WorkerList
            workers={stats.activeWorkers}
            selectedRepo={selectedWorkerRepo}
            onSelect={setSelectedWorkerRepo}
          />
        )}

        {tab === "events" && (
          <EventList
            events={visibleEvents}
            selectedRepo={selectedWorkerRepo}
            eventCount={eventCount}
            shownCount={visibleEvents.length}
            truncated={Boolean(detail.events_truncated)}
            onClearFilter={() => setSelectedWorkerRepo(null)}
          />
        )}

        {tab === "failures" && <FailureList failures={failures} />}

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

function BatchSummary({
  latestBatch,
  currentRunSpend,
  batchCatalogSpend,
  budgetUsd,
}: {
  latestBatch: Record<string, unknown> | null;
  currentRunSpend?: number | null;
  batchCatalogSpend?: number | null;
  budgetUsd?: number | null;
}) {
  if (!latestBatch) {
    return (
      <section className="rounded border border-border bg-bg px-3 py-2 text-sm text-fg-muted">
        No batch has started for this run yet.
      </section>
    );
  }
  return (
    <section className="rounded border border-border bg-bg px-3 py-2">
      <div className="text-xs uppercase tracking-wider text-fg-dim">Current batch</div>
      <div className="mt-2 grid grid-cols-3 gap-2 text-sm text-fg-muted">
        <div><span className="text-fg">{String(latestBatch.n || "-")}</span> repos</div>
        <div><span className="text-fg">{String(latestBatch.parallelism || "-")}</span> workers</div>
        <div><span className="text-fg">{String(latestBatch.stale_remaining || "-")}</span> queued</div>
      </div>
      <div className="mt-2 text-xs text-fg-dim">
        {currentRunSpend != null ? `${fmtMoney(currentRunSpend)} spent this run` : "Run spend pending"}
        {budgetUsd != null ? ` · $${budgetUsd} budget` : ""}
        {batchCatalogSpend != null ? ` · ${fmtMoney(batchCatalogSpend)} catalog spend` : ""}
      </div>
    </section>
  );
}

function WorkerList({
  workers,
  selectedRepo,
  onSelect,
  compact = false,
}: {
  workers: ActiveWorker[];
  selectedRepo?: string | null;
  onSelect: (repo: string) => void;
  compact?: boolean;
}) {
  if (!workers.length) {
    return (
      <section>
        <h3 className="mb-2 text-xs uppercase tracking-wider text-fg-dim">Workers</h3>
        <div className="rounded border border-border bg-bg px-3 py-2 text-sm text-fg-muted">
          No active workers are reporting yet.
        </div>
      </section>
    );
  }
  const visibleWorkers = compact ? workers.slice(0, 6) : workers;
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-xs uppercase tracking-wider text-fg-dim">Workers</h3>
        <span className="text-xs text-fg-dim">
          {compact && workers.length > visibleWorkers.length ? `${visibleWorkers.length} of ${workers.length}` : `${workers.length} active`}
        </span>
      </div>
      <div className="space-y-1">
        {visibleWorkers.map((worker) => (
          <button
            key={worker.repo}
            type="button"
            onClick={() => onSelect(worker.repo)}
            className={cn(
              "w-full rounded border border-border bg-bg px-3 py-2 text-left hover:border-accent/50",
              selectedRepo === worker.repo && "border-accent/60 bg-accent/10",
            )}
          >
            <div className="flex items-center gap-2">
              <span className="rounded border border-border px-1.5 py-0.5 font-mono text-[10px] text-fg-dim">
                {worker.workerId || "W"}
              </span>
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-fg">{worker.repo}</span>
              {worker.pid && <span className="text-[11px] text-fg-dim">PID {worker.pid}</span>}
            </div>
            <div className="mt-1 flex items-center gap-2 text-xs text-fg-dim">
              <span>{worker.elapsedSeconds != null ? formatSeconds(worker.elapsedSeconds) : "running"}</span>
              <span>·</span>
              <span className="truncate">{workerLabel(worker)}</span>
            </div>
          </button>
        ))}
      </div>
    </section>
  );
}

function CompletionList({
  completions,
  onReindexRepo,
  compact = false,
}: {
  completions: CompletionItem[];
  onReindexRepo: (repo: string) => void;
  compact?: boolean;
}) {
  if (!completions.length) {
    return (
      <section>
        <h3 className="mb-2 text-xs uppercase tracking-wider text-fg-dim">Recent completions</h3>
        <div className="rounded border border-border bg-bg px-3 py-2 text-sm text-fg-muted">
          No repo has completed in this run yet.
        </div>
      </section>
    );
  }
  const visible = compact ? completions.slice(0, 6) : completions;
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-xs uppercase tracking-wider text-fg-dim">Recent completions</h3>
        {completions.length > visible.length && <span className="text-xs text-fg-dim">Latest {visible.length} of {completions.length}</span>}
      </div>
      <div className="space-y-1">
        {visible.map((repo, index) => (
          <div key={`${repo.repo}-${repo.reason || "done"}-${index}`} className="rounded border border-border bg-bg px-3 py-2">
            <div className="flex items-center gap-2 text-fg">
              <GitBranch size={13} className="text-fg-dim" />
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{repo.repo}</span>
              <StatusBadge status={repo.status || "done"} />
              <button
                type="button"
                onClick={() => onReindexRepo(repo.repo)}
                className="inline-flex items-center gap-1 rounded border border-border px-1.5 py-0.5 text-[11px] text-fg-muted hover:text-fg"
              >
                <RotateCw size={11} /> Re-index
              </button>
            </div>
            <div className="mt-1 text-xs text-fg-dim">
              {[
                repo.reason || "crawler extraction",
                repo.duration_seconds ? formatSeconds(repo.duration_seconds) : null,
                repo.cost_usd != null ? fmtMoney(repo.cost_usd) : null,
              ].filter(Boolean).join(" · ")}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function FailureList({ failures }: { failures: FailureItem[] }) {
  if (!failures.length) {
    return (
      <section>
        <h3 className="mb-2 text-xs uppercase tracking-wider text-fg-dim">Needs attention</h3>
        <div className="rounded border border-border bg-bg px-3 py-2 text-sm text-fg-muted">
          No failures reported for this run.
        </div>
      </section>
    );
  }
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-xs uppercase tracking-wider text-fg-dim">Needs attention</h3>
        <span className="text-xs text-fg-dim">{failures.length} failure{failures.length === 1 ? "" : "s"}</span>
      </div>
      <div className="space-y-1">
        {failures.map((failure, index) => (
          <div key={`${failure.repo}-${failure.event || failure.status}-${index}`} className="rounded border border-red-500/30 bg-red-500/10 px-3 py-2">
            <div className="flex items-center gap-2 text-fg">
              <XCircle size={13} className="text-red-400" />
              <span className="min-w-0 flex-1 truncate font-mono text-xs">{failure.repo}</span>
              <span className="rounded border border-red-500/30 bg-red-500/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wider text-red-300">
                {failure.category}
              </span>
            </div>
            <div className="mt-1 text-xs text-fg-dim">
              {[failure.status.replaceAll("_", " "), failure.detail].filter(Boolean).join(" · ")}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function EventList({
  events,
  selectedRepo,
  eventCount,
  shownCount,
  truncated,
  onClearFilter,
}: {
  events: Array<Record<string, unknown>>;
  selectedRepo?: string | null;
  eventCount: number;
  shownCount: number;
  truncated: boolean;
  onClearFilter: () => void;
}) {
  if (!events.length) {
    return (
      <section>
        <h3 className="mb-2 text-xs uppercase tracking-wider text-fg-dim">Event stream</h3>
        <div className="rounded border border-border bg-bg px-3 py-2 text-sm text-fg-muted">
          {selectedRepo ? `No retained events for ${selectedRepo}.` : "No retained events for this run yet."}
        </div>
      </section>
    );
  }
  return (
    <section>
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-xs uppercase tracking-wider text-fg-dim">Event stream</h3>
        <span className="text-xs text-fg-dim">
          Latest {shownCount} of {eventCount}{truncated ? " retained" : ""}
        </span>
      </div>
      {selectedRepo && (
        <button
          type="button"
          onClick={onClearFilter}
          className="mb-2 rounded border border-border px-2 py-1 text-xs text-fg-muted hover:text-fg"
        >
          Showing {selectedRepo}. Clear filter
        </button>
      )}
      <ol className="space-y-2">
        {events.map((event, index) => (
          <li key={index} className="rounded border border-border bg-bg px-3 py-2">
            <div className="flex items-start gap-2">
              <TerminalSquare size={13} className="mt-0.5 shrink-0 text-fg-dim" />
              <div className="min-w-0 flex-1">
                <div className="text-sm text-fg">{formatEvent(event)}</div>
                <div className="mt-1 text-xs text-fg-dim">{formatDate(eventTimestamp(event))}</div>
              </div>
            </div>
          </li>
        ))}
      </ol>
    </section>
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
