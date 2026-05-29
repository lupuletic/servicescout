import { type ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, Check, GitBranch, KeyRound, Loader2, Rocket, Search, ShieldCheck, Settings2, X } from "lucide-react";
import { Card, CardTitle, Button, Input, PageHeader } from "@/components/ui";
import { cn } from "@/lib/cn";

type Org = { login: string };
type Repo = { name: string; full_name: string; description: string; language: string };
type AuditEntry = { ts: string; action: string; token_stored?: boolean; repo?: string };

async function postJSON<T>(url: string, body: unknown): Promise<T> {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error((data as { detail?: string }).detail || `${r.status} ${r.statusText}`);
  return data as T;
}

export function OnboardingPage() {
  const [token, setToken] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [login, setLogin] = useState<string | null>(null);
  const [orgs, setOrgs] = useState<Org[]>([]);
  const [selectedOrgs, setSelectedOrgs] = useState<Set<string>>(new Set());
  const [reposByOrg, setReposByOrg] = useState<Record<string, Repo[]>>({});
  const [loadingOrg, setLoadingOrg] = useState<string | null>(null);
  const [repoFilter, setRepoFilter] = useState("");
  const [seeds, setSeeds] = useState<Set<string>>(new Set());
  const [budget, setBudget] = useState(25);
  const [maxRounds, setMaxRounds] = useState(5);
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [audit, setAudit] = useState<AuditEntry[]>([]);

  const loadAudit = () =>
    fetch("/api/audit?limit=10")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (d?.entries) setAudit(d.entries); })
      .catch(() => undefined);

  // Prefill from the saved config so re-running shows existing seeds/budget.
  useEffect(() => {
    fetch("/api/workspace/config")
      .then((r) => (r.ok ? r.json() : null))
      .then((cfg) => {
        if (!cfg) return;
        if (Array.isArray(cfg.orgs)) setSelectedOrgs(new Set(cfg.orgs));
        if (Array.isArray(cfg.seeds)) setSeeds(new Set(cfg.seeds));
        if (typeof cfg.budget_usd === "number") setBudget(cfg.budget_usd);
        if (cfg.scope && typeof cfg.scope.max_discovery_rounds === "number") setMaxRounds(cfg.scope.max_discovery_rounds);
      })
      .catch(() => undefined);
    loadAudit();
  }, []);

  async function connect() {
    setConnecting(true);
    setError(null);
    try {
      const data = await postJSON<{ login: string; orgs: Org[] }>("/api/github/validate", { token });
      setLogin(data.login);
      setOrgs(data.orgs);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setConnecting(false);
    }
  }

  async function toggleOrg(org: string) {
    const next = new Set(selectedOrgs);
    if (next.has(org)) {
      next.delete(org);
    } else {
      next.add(org);
      if (!reposByOrg[org]) {
        setLoadingOrg(org);
        try {
          const data = await postJSON<{ repos: Repo[] }>("/api/github/repos", { token, org });
          setReposByOrg((prev) => ({ ...prev, [org]: data.repos }));
        } catch (e) {
          setError(e instanceof Error ? e.message : String(e));
        } finally {
          setLoadingOrg(null);
        }
      }
    }
    setSelectedOrgs(next);
  }

  function toggleSeed(fullName: string) {
    const next = new Set(seeds);
    if (next.has(fullName)) next.delete(fullName);
    else next.add(fullName);
    setSeeds(next);
  }

  async function saveAndCrawl() {
    setSaving(true);
    setError(null);
    setResult(null);
    try {
      await postJSON("/api/workspace/config", {
        orgs: [...selectedOrgs],
        seeds: [...seeds],
        discover: true,
        max_discovery_rounds: maxRounds,
        budget_usd: budget,
        token: token || undefined,
      });
      await postJSON("/api/crawl/trigger", {});
      setResult("Crawl started. Follow it on the Activity page.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
      loadAudit();
    }
  }

  const visibleRepos = (org: string): Repo[] => {
    const repos = reposByOrg[org] || [];
    const q = repoFilter.trim().toLowerCase();
    return q ? repos.filter((r) => r.full_name.toLowerCase().includes(q) || r.description.toLowerCase().includes(q)) : repos;
  };

  const seedList = [...seeds].sort();
  const selectedOrgList = [...selectedOrgs].sort();
  const canCrawl = seeds.size > 0 && budget > 0 && maxRounds >= 0 && !saving;

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Add seed"
        description="Choose entry-point repos, set a spend guardrail, then crawl downstream dependencies to enrich the current graph."
        actions={
          <Link to="/activity" className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-sm text-fg hover:bg-bg-elevated">
            <Activity size={14} /> Activity
          </Link>
        }
      />

      <div className="min-h-0 overflow-auto p-4 lg:p-6">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="space-y-4">
            <Card className="overflow-hidden p-0">
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
                <div>
                  <CardTitle className="mb-1 flex items-center gap-2">
                    <ShieldCheck size={14} /> GitHub access
                  </CardTitle>
                  <p className="max-w-3xl text-sm text-fg-muted">
                    Connect only when you need to browse or change repositories. Use a fine-grained, read-only token with
                    <code className="mx-1 rounded border border-border bg-bg px-1 py-0.5">Contents: read</code>
                    <code className="mx-1 rounded border border-border bg-bg px-1 py-0.5">Metadata: read</code>
                    and <code className="mx-1 rounded border border-border bg-bg px-1 py-0.5">Organization: read</code>.
                  </p>
                </div>
                <StatusPill active={Boolean(login)}>
                  {login ? `Connected as ${login}` : "Not connected"}
                </StatusPill>
              </div>
              <div className="grid gap-3 px-4 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
                <label className="text-xs font-medium uppercase tracking-[0.04em] text-fg-dim">
                  Read-only GitHub token
                  <Input
                    id="github-token"
                    type="password"
                    placeholder="github_pat_... (read-only)"
                    value={token}
                    onChange={(e) => setToken(e.target.value)}
                    className="mt-1 font-mono"
                    autoComplete="off"
                  />
                </label>
                <Button onClick={connect} disabled={!token || connecting} className="h-9">
                  {connecting ? <Loader2 className="animate-spin" size={16} /> : <KeyRound size={16} />}
                  {login ? "Reconnect" : "Connect"}
                </Button>
              </div>
            </Card>

            <Card className="overflow-hidden p-0">
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border px-4 py-3">
                <div>
                  <CardTitle className="mb-1 flex items-center gap-2">
                    <GitBranch size={14} /> Journey seeds
                  </CardTitle>
                  <p className="text-sm text-fg-muted">
                    Start from storefronts, mobile apps, APIs, or other entry points. Discovery follows downstream dependencies.
                  </p>
                </div>
                <StatusPill active={seeds.size > 0}>{seeds.size} selected</StatusPill>
              </div>

              <div className="space-y-4 px-4 py-3">
                {seeds.size > 0 ? (
                  <div className="max-h-32 overflow-auto rounded-md border border-border bg-bg/40 p-2">
                    <div className="flex flex-wrap gap-1.5">
                      {seedList.map((seed) => (
                        <span key={seed} className="inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs font-mono text-fg">
                          <span className="truncate">{seed}</span>
                          <button
                            type="button"
                            onClick={() => toggleSeed(seed)}
                            aria-label={`Remove ${seed}`}
                            className="-mr-1 grid h-8 w-8 shrink-0 place-items-center rounded text-fg-dim hover:bg-bg hover:text-red-400 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70"
                          >
                            <X size={11} />
                          </button>
                        </span>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="rounded-md border border-dashed border-border px-3 py-4 text-sm text-fg-muted">
                    No seed repos selected yet. Connect GitHub to browse repositories, or keep an existing saved seed list.
                  </div>
                )}

                {orgs.length === 0 && seeds.size > 0 && (
                  <p className="text-xs text-fg-dim">Current seeds are loaded from the saved workspace. Connect GitHub to add or replace them.</p>
                )}

                {orgs.length > 0 && (
                  <div className="space-y-3">
                    <div className="flex flex-wrap gap-2">
                      {orgs.map((org) => (
                        <Button
                          key={org.login}
                          variant={selectedOrgs.has(org.login) ? "default" : "outline"}
                          onClick={() => toggleOrg(org.login)}
                          className="h-8"
                        >
                          {selectedOrgs.has(org.login) && <Check size={14} />} {org.login}
                        </Button>
                      ))}
                    </div>

                    {selectedOrgList.length > 0 && (
                      <div className="relative max-w-lg">
                        <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-fg-dim" />
                        <Input
                          placeholder="Filter repositories"
                          value={repoFilter}
                          onChange={(e) => setRepoFilter(e.target.value)}
                          className="pl-8"
                        />
                      </div>
                    )}

                    {selectedOrgList.map((org) => (
                      <div key={org} className="space-y-1.5">
                        <div className="flex items-center justify-between text-xs uppercase tracking-wider text-fg-dim">
                          <span>{org}</span>
                          <span>{visibleRepos(org).length} repos</span>
                        </div>
                        {loadingOrg === org && (
                          <p className="flex items-center gap-1 text-sm text-fg-muted">
                            <Loader2 className="animate-spin" size={14} /> Loading repos...
                          </p>
                        )}
                        <div className="max-h-72 overflow-auto rounded-md border border-border">
                          {visibleRepos(org).map((repo) => (
                            <label
                              key={repo.full_name}
                              className={cn(
                                "grid cursor-pointer grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 border-b border-border px-3 py-2 text-sm last:border-b-0 hover:bg-bg",
                                seeds.has(repo.full_name) && "bg-accent/10",
                              )}
                            >
                              <input type="checkbox" checked={seeds.has(repo.full_name)} onChange={() => toggleSeed(repo.full_name)} />
                              <span className="min-w-0">
                                <span className="block truncate font-mono text-fg">{repo.name}</span>
                                {repo.description && <span className="block truncate text-xs text-fg-dim">{repo.description}</span>}
                              </span>
                              {repo.language && <span className="rounded border border-border px-1.5 py-0.5 text-[10px] text-fg-dim">{repo.language}</span>}
                            </label>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </Card>
          </div>

          <aside className="space-y-4 xl:sticky xl:top-4 xl:self-start">
            <Card className="space-y-4">
              <div>
                <CardTitle className="mb-1 flex items-center gap-2">
                  <Settings2 size={14} /> Crawl plan
                </CardTitle>
                <p className="text-sm text-fg-muted">Save seed changes and start a manual enrichment crawl immediately.</p>
              </div>

              <div className="grid grid-cols-2 gap-2">
                <PlanMetric label="Seeds" value={seeds.size} />
                <PlanMetric label="Orgs" value={selectedOrgs.size || "-"} />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <label className="text-xs uppercase tracking-wider text-fg-dim">
                  Budget (USD)
                  <Input type="number" min={1} value={budget} onChange={(e) => setBudget(Number(e.target.value))} className="mt-1" />
                </label>
                <label className="text-xs uppercase tracking-wider text-fg-dim">
                  Rounds
                  <Input type="number" min={0} value={maxRounds} onChange={(e) => setMaxRounds(Number(e.target.value))} className="mt-1" />
                </label>
              </div>

              <Button onClick={saveAndCrawl} disabled={!canCrawl} className="h-10 w-full">
                {saving ? <Loader2 className="animate-spin" size={16} /> : <Rocket size={16} />} Save and start crawl
              </Button>

              {seeds.size === 0 && <p className="text-xs text-fg-dim">Select at least one seed repo before crawling.</p>}
              {result && (
                <p className="text-sm text-emerald-400">
                  {result} <Link to="/activity" className="underline">Open Activity</Link>
                </p>
              )}
              {error && <p className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</p>}
            </Card>

            {audit.length > 0 && (
              <Card className="space-y-2">
                <CardTitle>Recent activity</CardTitle>
                <ul className="divide-y divide-border text-sm">
                  {audit.map((entry, index) => (
                    <li key={index} className="flex items-start justify-between gap-3 py-2">
                      <span className="min-w-0 truncate font-mono text-xs text-fg">
                        {entry.action}{entry.repo ? ` · ${entry.repo}` : ""}{entry.token_stored ? " · token stored" : ""}
                      </span>
                      <span className="shrink-0 text-xs text-fg-dim">{new Date(entry.ts).toLocaleDateString()}</span>
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}

function StatusPill({ active, children }: { active: boolean; children: ReactNode }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-2 py-1 text-xs font-medium",
        active ? "border-accent/35 bg-accent/15 text-accent" : "border-border bg-bg text-fg-dim",
      )}
    >
      {children}
    </span>
  );
}

function PlanMetric({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-bg/45 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wider text-fg-dim">{label}</div>
      <div className="mt-0.5 text-lg font-semibold text-fg">{value}</div>
    </div>
  );
}
