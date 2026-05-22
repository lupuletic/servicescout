import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { KeyRound, Loader2, Check, Rocket } from "lucide-react";
import { Card, CardTitle, Button, Input, PageHeader } from "@/components/ui";

type Org = { login: string };
type Repo = { name: string; full_name: string; description: string; language: string };

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

  // Prefill from the current saved config so re-onboarding shows existing state.
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
        title="Onboard a workspace"
        description="Connect GitHub, pick your journey-seed repos (storefronts, mobile, entry points), and crawl downstream."
      />

      {/* Step 1: connect */}
      <Card className="space-y-3">
        <CardTitle>1 · Connect GitHub</CardTitle>
        <p className="text-sm text-muted-foreground">
          Paste a Personal Access Token (scopes: <code>repo</code> + <code>read:org</code>). It's used to list your
          orgs/repos and authorize the crawl — stored server-side, never shown again.
        </p>
        <div className="flex gap-2">
          <Input
            type="password"
            placeholder="ghp_…"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="max-w-md font-mono"
          />
          <Button onClick={connect} disabled={!token || connecting}>
            {connecting ? <Loader2 className="animate-spin" size={16} /> : <KeyRound size={16} />}
            {login ? "Reconnect" : "Connect"}
          </Button>
        </div>
        {login && <p className="text-sm text-emerald-600 flex items-center gap-1"><Check size={14} /> Connected as <strong>{login}</strong> — {orgs.length} org(s) readable.</p>}
      </Card>

      {/* Step 2: orgs + seeds */}
      {orgs.length > 0 && (
        <Card className="space-y-3">
          <CardTitle>2 · Pick orgs &amp; journey seeds</CardTitle>
          <div className="flex flex-wrap gap-2">
            {orgs.map((o) => (
              <Button
                key={o.login}
                variant={selectedOrgs.has(o.login) ? "default" : "outline"}
                onClick={() => toggleOrg(o.login)}
              >
                {selectedOrgs.has(o.login) && <Check size={14} />} {o.login}
              </Button>
            ))}
          </div>
          {[...selectedOrgs].length > 0 && (
            <Input
              placeholder="Filter repos…"
              value={repoFilter}
              onChange={(e) => setRepoFilter(e.target.value)}
              className="max-w-md"
            />
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
          {seeds.size > 0 && <p className="text-sm">{seeds.size} seed(s) selected — the crawl extracts these, then follows their dependencies downstream.</p>}
        </Card>
      )}

      {/* Step 3: scope + start */}
      {seeds.size > 0 && (
        <Card className="space-y-3">
          <CardTitle>3 · Budget &amp; crawl</CardTitle>
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
    </div>
  );
}
