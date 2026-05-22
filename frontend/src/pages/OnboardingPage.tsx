import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { KeyRound, Loader2, Check, Rocket, X } from "lucide-react";
import { Card, CardTitle, Button, Input, PageHeader } from "@/components/ui";

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
      setResult("Crawl started — follow it on the Activity page.");
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

  return (
    <div className="space-y-6">
      <PageHeader
        title="Set up a crawl"
        description="Connect GitHub, pick journey-seed repos (storefronts, mobile, entry points), and crawl downstream from them."
      />

      {/* Step 1 · connect */}
      <Card className="space-y-3">
        <CardTitle>1 · Connect GitHub</CardTitle>
        <p className="text-sm text-muted-foreground">
          Paste a <strong>fine-grained, read-only</strong> Personal Access Token — repository permissions{" "}
          <code>Contents: read</code> + <code>Metadata: read</code>, and <code>Organization: read</code>. ServiceScout
          only clones and reads; it never needs write access. The token lists your orgs/repos and authorizes the crawl;
          it's stored server-side and never shown again.
        </p>
        <div className="flex gap-2">
          <Input
            type="password"
            placeholder="github_pat_… (read-only)"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="max-w-md font-mono"
            autoComplete="off"
          />
          <Button onClick={connect} disabled={!token || connecting}>
            {connecting ? <Loader2 className="animate-spin" size={16} /> : <KeyRound size={16} />}
            {login ? "Reconnect" : "Connect"}
          </Button>
        </div>
        {login && (
          <p className="text-sm text-emerald-600 flex items-center gap-1">
            <Check size={14} /> Connected as <strong>{login}</strong> — {orgs.length} org(s) readable.
          </p>
        )}
      </Card>

      {/* Step 2 · seeds — ALWAYS visible so prefilled seeds never hide the picker */}
      <Card className="space-y-3">
        <CardTitle>2 · Journey seeds</CardTitle>
        {seeds.size > 0 ? (
          <div className="flex flex-wrap gap-2">
            {[...seeds].map((s) => (
              <span key={s} className="inline-flex items-center gap-1 rounded-full bg-muted px-2.5 py-1 text-xs font-mono">
                {s}
                <button onClick={() => toggleSeed(s)} aria-label={`Remove ${s}`} className="hover:text-red-600">
                  <X size={12} />
                </button>
              </span>
            ))}
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No seeds yet — connect above, pick your orgs, then check the entry-point repos.</p>
        )}

        {orgs.length === 0 && seeds.size > 0 && (
          <p className="text-xs text-muted-foreground">Connect to add or change seeds.</p>
        )}

        {orgs.length > 0 && (
          <>
            <div className="flex flex-wrap gap-2 pt-1">
              {orgs.map((o) => (
                <Button key={o.login} variant={selectedOrgs.has(o.login) ? "default" : "outline"} onClick={() => toggleOrg(o.login)}>
                  {selectedOrgs.has(o.login) && <Check size={14} />} {o.login}
                </Button>
              ))}
            </div>
            {[...selectedOrgs].length > 0 && (
              <Input placeholder="Filter repos…" value={repoFilter} onChange={(e) => setRepoFilter(e.target.value)} className="max-w-md" />
            )}
            {[...selectedOrgs].map((org) => (
              <div key={org} className="space-y-1">
                <div className="text-xs font-semibold uppercase text-muted-foreground">{org}</div>
                {loadingOrg === org && <p className="text-sm flex items-center gap-1"><Loader2 className="animate-spin" size={14} /> loading repos…</p>}
                <div className="max-h-64 overflow-auto rounded border border-border divide-y divide-border">
                  {visibleRepos(org).map((r) => (
                    <label key={r.full_name} className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-muted/40 cursor-pointer">
                      <input type="checkbox" checked={seeds.has(r.full_name)} onChange={() => toggleSeed(r.full_name)} />
                      <span className="font-mono">{r.name}</span>
                      {r.language && <span className="text-xs text-muted-foreground">{r.language}</span>}
                      <span className="text-xs text-muted-foreground truncate">{r.description}</span>
                    </label>
                  ))}
                </div>
              </div>
            ))}
          </>
        )}
      </Card>

      {/* Step 3 · budget & crawl */}
      {seeds.size > 0 && (
        <Card className="space-y-3">
          <CardTitle>3 · Budget &amp; crawl</CardTitle>
          <p className="text-sm text-muted-foreground">
            The crawl extracts your {seeds.size} seed(s), then follows their dependencies downstream — stopping at the
            budget or after the discovery rounds.
          </p>
          <div className="flex flex-wrap items-end gap-4">
            <label className="text-sm">
              Budget (USD)
              <Input type="number" min={1} value={budget} onChange={(e) => setBudget(Number(e.target.value))} className="w-28" />
            </label>
            <label className="text-sm">
              Max discovery rounds
              <Input type="number" min={0} value={maxRounds} onChange={(e) => setMaxRounds(Number(e.target.value))} className="w-28" />
            </label>
            <Button onClick={saveAndCrawl} disabled={saving || selectedOrgs.size === 0}>
              {saving ? <Loader2 className="animate-spin" size={16} /> : <Rocket size={16} />} Save &amp; crawl
            </Button>
          </div>
          {result && (
            <p className="text-sm text-emerald-600">
              {result} <Link to="/activity" className="underline">Go to Activity →</Link>
            </p>
          )}
        </Card>
      )}

      {error && <p className="text-sm text-red-600">⚠ {error}</p>}

      {/* Audit trail — config changes, token storage, crawl triggers */}
      {audit.length > 0 && (
        <Card className="space-y-2">
          <CardTitle>Recent activity</CardTitle>
          <ul className="text-sm divide-y divide-border">
            {audit.map((a, i) => (
              <li key={i} className="flex items-center justify-between gap-3 py-1.5">
                <span className="font-mono text-xs">
                  {a.action}{a.repo ? ` · ${a.repo}` : ""}{a.token_stored ? " · token stored" : ""}
                </span>
                <span className="text-xs text-muted-foreground">{new Date(a.ts).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
