import { useState, type ReactNode } from "react";
import useSWR from "swr";
import { useParams, Link } from "react-router-dom";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { type EntityRecord } from "@/lib/api";
import { KindBadge, PageHeader } from "@/components/ui";
import { MiniGraph } from "@/components/MiniGraph";
import { cn } from "@/lib/cn";

type EntityFull = EntityRecord & {
  metadata: any;
  spec: any;
  evidence: any[];
};

type TabKey = "overview" | "relations" | "evidence";

const TABS: Array<{ id: TabKey; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "relations", label: "Relations" },
  { id: "evidence", label: "Evidence" },
];

export function EntityPage() {
  const { ref } = useParams<{ ref: string }>();
  const decoded = ref ? decodeURIComponent(ref) : "";
  const [tab, setTab] = useState<TabKey>("overview");
  const { data, isLoading, error } = useSWR<EntityFull>(
    decoded ? `/api/entity/${encodeURIComponent(decoded)}` : null,
  );

  if (isLoading) {
    return <div className="p-6 text-fg-muted">Loading…</div>;
  }
  if (error || !data) {
    return <div className="p-6 text-fg-muted">Not found: {decoded}</div>;
  }

  const meta = data.metadata || {};
  const annotations = meta.annotations || {};
  const evidence = data.evidence || [];
  const sourceRepos: string[] = annotations.source_repos || [];

  return (
    <div className="h-full grid grid-rows-[auto_auto_1fr]">
      <PageHeader
        title={meta.name || data.ref}
        description={annotations.tagline}
        actions={
          <Link
            to="/entities"
            className="inline-flex items-center gap-1.5 text-sm text-fg-muted hover:text-fg"
          >
            <ArrowLeft size={14} /> Catalog
          </Link>
        }
      />
      {/* Tab bar */}
      <div className="flex items-center gap-1 px-6 border-b border-border bg-bg-elevated/40">
        <div className="flex items-center gap-2.5 mr-4">
          <KindBadge kind={data.kind} />
          <span className="text-xs text-fg-dim font-mono">{data.ref}</span>
        </div>
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "px-3 py-2.5 text-sm transition-colors border-b-2 -mb-px",
              tab === t.id
                ? "text-fg border-accent"
                : "text-fg-muted hover:text-fg border-transparent",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      <div className="overflow-auto">
        {tab === "overview" && (
          <OverviewTab entity={data} centerRef={decoded} />
        )}
        {tab === "relations" && (
          <RelationsTab centerRef={decoded} />
        )}
        {tab === "evidence" && (
          <EvidenceTab evidence={evidence} sourceRepos={sourceRepos} />
        )}
      </div>
    </div>
  );
}

// -------------------------------------------------------------------------- //
// Overview tab — AboutCard left, MiniGraph right, capability + glossary below.
// -------------------------------------------------------------------------- //

function OverviewTab({ entity, centerRef }: { entity: EntityFull; centerRef: string }) {
  const meta = entity.metadata || {};
  const annotations = meta.annotations || {};
  const spec = entity.spec || {};
  const domainAttributes = spec.domain_attributes || [];
  const glossary = spec.glossary || [];
  const sourceRepos: string[] = annotations.source_repos || [];

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_420px] gap-4">
        <AboutCard entity={entity} />
        <Card title="Neighbourhood">
          <p className="text-xs text-fg-dim mb-3">
            Direct dependencies (1 hop). Click a node to navigate.
          </p>
          <MiniGraph centerRef={centerRef} depth={1} height={340} />
        </Card>
      </div>

      {annotations.capability_sheet && (
        <Card title="Capability sheet">
          <div className="whitespace-pre-wrap text-sm text-fg-muted leading-relaxed font-sans">
            {annotations.capability_sheet}
          </div>
        </Card>
      )}

      {domainAttributes.length > 0 && (
        <Card title="Domain attributes">
          <div className="space-y-2">
            {domainAttributes.slice(0, 12).map((a: any) => (
              <div key={a.attribute} className="rounded border border-border bg-bg p-3 text-sm">
                <div className="font-mono text-fg">{a.attribute}</div>
                <div className="text-xs text-fg-dim mt-1">
                  {(a.values || []).slice(0, 12).join(" · ")}
                </div>
                {a.meaning && <div className="text-fg-muted mt-1">{a.meaning}</div>}
              </div>
            ))}
          </div>
        </Card>
      )}

      {glossary.length > 0 && (
        <Card title="Glossary">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2 text-sm">
            {glossary.slice(0, 20).map((g: any) => (
              <div key={g.term}>
                <span className="font-medium text-fg">{g.term}</span>
                <span className="text-fg-muted"> — {g.definition}</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      {sourceRepos.length > 0 && (
        <div className="flex items-center gap-2 text-sm">
          {sourceRepos.map((r) => (
            <a
              key={r}
              href={`https://github.com/${r}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 text-accent hover:underline"
            >
              <ExternalLink size={13} /> {r}
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function AboutCard({ entity }: { entity: EntityFull }) {
  const meta = entity.metadata || {};
  const annotations = meta.annotations || {};
  const spec = entity.spec || {};
  const sourceRepos: string[] = annotations.source_repos || [];
  const environments: string[] = spec.environments || [];
  const aliases: string[] = annotations.aliases || [];

  const rows: Array<[string, ReactNode]> = [];
  if (spec.type) rows.push(["Type", <span className="text-fg">{spec.type}</span>]);
  if (spec.runtime) rows.push(["Runtime", <span className="text-fg">{spec.runtime}</span>]);
  if (spec.lifecycle && spec.lifecycle !== "unknown")
    rows.push(["Lifecycle", <LifecycleBadge value={spec.lifecycle} />]);
  if (spec.system) rows.push(["System", <span className="text-fg">{spec.system}</span>]);
  if (spec.domain) rows.push(["Domain", <span className="text-fg">{spec.domain}</span>]);
  if (spec.category) rows.push(["Category", <span className="text-fg">{spec.category}</span>]);
  if (spec.owner && spec.owner !== "unknown")
    rows.push(["Owner", <span className="text-fg">{spec.owner}</span>]);
  if (spec.subcomponentOf)
    rows.push([
      "Subcomponent of",
      <Link
        to={`/entity/${encodeURIComponent(`Component:${spec.subcomponentOf}`)}`}
        className="text-accent hover:underline"
      >
        {spec.subcomponentOf}
      </Link>,
    ]);
  if (sourceRepos.length > 0)
    rows.push([
      "Source repo",
      <div className="space-y-0.5">
        {sourceRepos.map((r) => (
          <a key={r} href={`https://github.com/${r}`} target="_blank" rel="noreferrer" className="block text-accent hover:underline">
            {r}
          </a>
        ))}
      </div>,
    ]);
  if (environments.length > 0)
    rows.push([
      "Environments",
      <div className="flex flex-wrap gap-1">
        {environments.map((e) => (
          <span key={e} className="rounded border border-border bg-bg px-1.5 py-0.5 text-xs text-fg-muted">
            {e}
          </span>
        ))}
      </div>,
    ]);
  if (aliases.length > 0)
    rows.push([
      "Aliases",
      <div className="flex flex-wrap gap-1">
        {aliases.slice(0, 12).map((a) => (
          <span key={a} className="rounded border border-border bg-bg px-1.5 py-0.5 text-xs text-fg-muted">
            {a}
          </span>
        ))}
        {aliases.length > 12 && <span className="text-xs text-fg-dim">+{aliases.length - 12}</span>}
      </div>,
    ]);

  return (
    <Card title="About">
      {meta.description && (
        <p className="text-sm text-fg-muted mb-4">{meta.description}</p>
      )}
      <dl className="grid grid-cols-[120px_1fr] gap-x-4 gap-y-2 text-sm">
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-xs uppercase tracking-wider text-fg-dim pt-0.5">{k}</dt>
            <dd className="text-fg-muted">{v}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

function LifecycleBadge({ value }: { value: string }) {
  const styles: Record<string, string> = {
    production: "bg-green-500/15 text-green-400 border-green-500/30",
    experimental: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30",
    deprecated: "bg-red-500/15 text-red-400 border-red-500/30",
  };
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider",
        styles[value] || "bg-bg text-fg-muted border-border",
      )}
    >
      {value}
    </span>
  );
}

// -------------------------------------------------------------------------- //
// Relations tab — full Sigma graph centred on the entity at depth N.
// -------------------------------------------------------------------------- //

function RelationsTab({ centerRef }: { centerRef: string }) {
  const [depth, setDepth] = useState(2);
  return (
    <div className="p-6 max-w-7xl mx-auto">
      <Card title="Relations">
        <div className="flex items-center gap-3 mb-3 text-sm">
          <span className="text-fg-dim">Depth</span>
          {[1, 2, 3].map((d) => (
            <button
              key={d}
              onClick={() => setDepth(d)}
              className={cn(
                "rounded px-2.5 py-1 text-xs border",
                depth === d
                  ? "border-accent bg-accent/15 text-fg"
                  : "border-border text-fg-muted hover:text-fg",
              )}
            >
              {d}-hop
            </button>
          ))}
          <span className="ml-auto text-xs text-fg-dim">
            Click a node to navigate to its entity page.
          </span>
        </div>
        <MiniGraph centerRef={centerRef} depth={depth} height={560} />
      </Card>
    </div>
  );
}

// -------------------------------------------------------------------------- //
// Evidence tab — file:line list with snippets, grouped by source path.
// -------------------------------------------------------------------------- //

function EvidenceTab({
  evidence,
  sourceRepos,
}: {
  evidence: Array<{ path: string; line: number; snippet: string }>;
  sourceRepos: string[];
}) {
  if (evidence.length === 0) {
    return <div className="p-6 text-fg-muted">No file:line evidence captured for this entity.</div>;
  }
  const repo = sourceRepos[0] || "";
  return (
    <div className="p-6 max-w-5xl mx-auto space-y-3">
      <Card title="Evidence">
        <p className="text-xs text-fg-dim mb-3">
          File:line citations the LLM extractor recorded. Click to open on GitHub.
        </p>
        <ul className="space-y-2">
          {evidence.map((e, i) => {
            const url = repo ? `https://github.com/${repo}/blob/main/${e.path}#L${e.line}` : null;
            return (
              <li key={i} className="rounded border border-border bg-bg p-3 text-sm">
                {url ? (
                  <a href={url} target="_blank" rel="noreferrer" className="font-mono text-accent hover:underline text-xs">
                    {e.path}:{e.line}
                  </a>
                ) : (
                  <span className="font-mono text-xs text-fg-muted">
                    {e.path}:{e.line}
                  </span>
                )}
                {e.snippet && (
                  <pre className="mt-1.5 text-xs text-fg-muted whitespace-pre-wrap font-mono">
                    {e.snippet}
                  </pre>
                )}
              </li>
            );
          })}
        </ul>
      </Card>
    </div>
  );
}

// -------------------------------------------------------------------------- //
// Shared local card primitive (don't reuse the global one — this one has the
// title row baked in for layout consistency).
// -------------------------------------------------------------------------- //

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-bg-elevated p-5">
      <h2 className="text-xs uppercase tracking-wider text-fg-dim mb-4">{title}</h2>
      {children}
    </section>
  );
}
