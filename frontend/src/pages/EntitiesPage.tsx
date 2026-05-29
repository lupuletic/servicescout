import { useState, useMemo } from "react";
import useSWR from "swr";
import { useSearchParams, useNavigate } from "react-router-dom";
import { type EntityRecord } from "@/lib/api";
import { SearchX } from "lucide-react";
import { Button, ConfidenceBadge, EmptyState, Input, KindBadge, PageHeader } from "@/components/ui";
import { cn } from "@/lib/cn";
import { KIND_META, kindLabel } from "@/lib/catalogLabels";

type FacetEntry = { value: string; count: number };
type FacetsPayload = {
  kind: FacetEntry[];
  type: FacetEntry[];
  owner: FacetEntry[];
  lifecycle: FacetEntry[];
  environment: FacetEntry[];
  tag: FacetEntry[];
  runtime: FacetEntry[];
  confidence: FacetEntry[];
};
type EntitiesPayload = {
  entities: EntityRecord[];
  count: number;
  returned?: number;
  limit?: number;
  truncated?: boolean;
};

// URL params we sync. Each is a comma-separated list (kind=Component,Provider).
const FACET_KEYS = ["kind", "type", "owner", "lifecycle", "environment", "tag", "runtime", "confidence"] as const;
type FacetKey = (typeof FACET_KEYS)[number];

function readSelection(sp: URLSearchParams, key: FacetKey): Set<string> {
  const raw = sp.get(key);
  if (!raw) return new Set();
  return new Set(raw.split(",").map((s) => s.trim()).filter(Boolean));
}

export function EntitiesPage() {
  const [sp, setSp] = useSearchParams();
  const [query, setQuery] = useState(sp.get("q") || "");
  const navigate = useNavigate();

  const selections = useMemo<Record<FacetKey, Set<string>>>(() => {
    return FACET_KEYS.reduce((acc, key) => {
      acc[key] = readSelection(sp, key);
      return acc;
    }, {} as Record<FacetKey, Set<string>>);
  }, [sp]);

  const { data: facets } = useSWR<FacetsPayload>("/api/facets");

  const entitiesUrl = useMemo(() => {
    const params = new URLSearchParams();
    // kind is single-select (server only supports one); pick the first.
    const kindSel = Array.from(selections.kind);
    if (kindSel.length === 1) params.set("kind", kindSel[0]);
    for (const key of ["owner", "lifecycle", "environment", "tag", "runtime", "confidence"] as FacetKey[]) {
      Array.from(selections[key]).forEach((v) => params.append(key, v));
    }
    if (query.trim()) params.set("query", query.trim());
    params.set("limit", "5000");
    return `/api/entities?${params.toString()}`;
  }, [selections, query]);

  const { data, isLoading, error } = useSWR<EntitiesPayload>(entitiesUrl);

  const toggle = (key: FacetKey, value: string) => {
    const next = new URLSearchParams(sp);
    const current = readSelection(sp, key);
    if (current.has(value)) {
      current.delete(value);
    } else {
      current.add(value);
    }
    if (current.size > 0) {
      next.set(key, Array.from(current).join(","));
    } else {
      next.delete(key);
    }
    setSp(next, { replace: true });
  };

  const clearFacet = (key: FacetKey) => {
    const next = new URLSearchParams(sp);
    next.delete(key);
    setSp(next, { replace: true });
  };

  const clearAll = () => {
    const next = new URLSearchParams();
    if (query.trim()) next.set("q", query.trim());
    setSp(next, { replace: true });
  };

  const clearSearchAndFilters = () => {
    setQuery("");
    setSp(new URLSearchParams(), { replace: true });
  };

  const rows = data?.entities ?? [];
  const returned = data?.returned ?? rows.length;
  const total = data?.count ?? 0;
  const catalogDescription = data
    ? data.truncated
      ? `${returned} of ${total} entities`
      : `${total} entities`
    : "Loading…";
  const anyFacetActive = FACET_KEYS.some((k) => selections[k].size > 0);

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Catalog"
        description={catalogDescription}
        actions={
          <Input
            placeholder="Search names, aliases, source repos…"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              const next = new URLSearchParams(sp);
              if (e.target.value.trim()) next.set("q", e.target.value.trim());
              else next.delete("q");
              setSp(next, { replace: true });
            }}
            className="w-full sm:w-80"
          />
        }
      />
      <div className="grid min-h-0 grid-cols-1 lg:grid-cols-[280px_minmax(0,1fr)]">
        <aside className="max-h-[42vh] overflow-auto border-b border-border bg-bg-elevated/40 p-4 text-sm lg:max-h-none lg:border-b-0 lg:border-r">
          <div className="space-y-5">
          {anyFacetActive && (
            <button
              onClick={clearAll}
              className="w-full rounded-md px-2 py-1 text-left text-xs text-fg-dim hover:bg-bg hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70"
            >
              Clear all filters
            </button>
          )}
          <FacetGroup
            title="Entity type"
            entries={facets?.kind || []}
            selected={selections.kind}
            onToggle={(v) => toggle("kind", v)}
            onClear={() => clearFacet("kind")}
            renderLabel={(v) => kindLabel(v, true)}
            renderHint={(v) => KIND_META[v]?.description}
          />
          <FacetGroup title="Confidence"    entries={facets?.confidence  || []} selected={selections.confidence}  onToggle={(v) => toggle("confidence", v)}  onClear={() => clearFacet("confidence")} renderLabel={(v) => v} max={4} />
          <FacetGroup title="Lifecycle"     entries={facets?.lifecycle   || []} selected={selections.lifecycle}   onToggle={(v) => toggle("lifecycle", v)}   onClear={() => clearFacet("lifecycle")} renderLabel={(v) => v} />
          <FacetGroup title="Runtime"       entries={facets?.runtime     || []} selected={selections.runtime}     onToggle={(v) => toggle("runtime", v)}     onClear={() => clearFacet("runtime")} renderLabel={(v) => v} />
          <FacetGroup title="Environment"   entries={facets?.environment || []} selected={selections.environment} onToggle={(v) => toggle("environment", v)} onClear={() => clearFacet("environment")} renderLabel={(v) => v} />
          <FacetGroup title="Owner"         entries={facets?.owner       || []} selected={selections.owner}       onToggle={(v) => toggle("owner", v)}       onClear={() => clearFacet("owner")} renderLabel={(v) => v} max={12} />
          <FacetGroup title="Type"          entries={facets?.type        || []} selected={selections.type}        onToggle={(v) => toggle("type", v)}        onClear={() => clearFacet("type")} renderLabel={(v) => v} max={10} />
          <FacetGroup title="Tag"           entries={facets?.tag         || []} selected={selections.tag}         onToggle={(v) => toggle("tag", v)}         onClear={() => clearFacet("tag")} renderLabel={(v) => v} max={12} />
          </div>
        </aside>

        <div className="min-w-0 overflow-auto">
          {isLoading && <div className="p-6 text-fg-muted">Loading entities...</div>}
          {error && (
            <div className="p-6">
              <EmptyState
                icon={SearchX}
                title="Catalog failed to load"
                description="The catalog API did not return entities. Refresh the page or check whether the dashboard API is running."
                actions={<Button variant="outline" onClick={() => window.location.reload()}>Reload catalog</Button>}
              />
            </div>
          )}
          {!isLoading && !error && rows.length === 0 && (
            <div className="p-6">
              <EmptyState
                icon={SearchX}
                title="No entities match"
                description="Adjust the search terms or clear filters to return to the full catalog."
                actions={<Button variant="outline" onClick={clearSearchAndFilters}>Clear search and filters</Button>}
              />
            </div>
          )}
          <table className="w-full min-w-[980px] table-fixed text-sm">
            <colgroup>
              <col className="w-36" />
              <col className="w-72" />
              <col className="w-32" />
              <col />
              <col className="w-56" />
            </colgroup>
            <thead className="sticky top-0 bg-bg-elevated text-xs uppercase tracking-wider text-fg-dim">
              <tr>
                <th className="text-left px-6 py-2.5 font-medium">Type</th>
                <th className="text-left px-3 py-2.5 font-medium">Name</th>
                <th className="text-left px-3 py-2.5 font-medium">Confidence</th>
                <th className="text-left px-3 py-2.5 font-medium">Tagline</th>
                <th className="text-left px-3 py-2.5 font-medium">Repo</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr
                  key={e.ref}
                  onClick={() => navigate(`/entity/${encodeURIComponent(e.ref)}`)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      navigate(`/entity/${encodeURIComponent(e.ref)}`);
                    }
                  }}
                  tabIndex={0}
                  title={`Open ${e.name}`}
                  className="cursor-pointer border-t border-border hover:bg-bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/70"
                >
                  <td className="px-6 py-2.5"><KindBadge kind={e.kind} /></td>
                  <td className="px-3 py-2.5 font-medium text-fg truncate">{e.name}</td>
                  <td className="px-3 py-2.5 whitespace-nowrap"><ConfidenceBadge confidence={e.confidence} /></td>
                  <td className="px-3 py-2.5 text-fg-muted truncate">{e.tagline || e.description || ""}</td>
                  <td className="px-3 py-2.5 text-fg-dim text-xs font-mono truncate">{e.source_repos?.[0] || ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function FacetGroup({
  title,
  entries,
  selected,
  onToggle,
  onClear,
  renderLabel,
  renderHint,
  max = 8,
}: {
  title: string;
  entries: FacetEntry[];
  selected: Set<string>;
  onToggle: (v: string) => void;
  onClear: () => void;
  renderLabel: (v: string) => string;
  renderHint?: (v: string) => string | undefined;
  max?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  if (entries.length === 0) return null;
  const visible = expanded ? entries : entries.slice(0, max);
  return (
    <div>
      <div className="flex items-center justify-between mb-1.5">
        <h3 className="text-xs uppercase tracking-wider text-fg-dim">{title}</h3>
        {selected.size > 0 && (
          <button onClick={onClear} className="rounded px-1 text-[10px] text-fg-dim hover:bg-bg hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70">clear</button>
        )}
      </div>
      <ul className="space-y-0.5">
        {visible.map((e) => {
          const isOn = selected.has(e.value);
          return (
            <li key={e.value}>
              <button
                onClick={() => onToggle(e.value)}
                className={cn(
                  "flex min-h-9 w-full items-start justify-between gap-2 rounded px-2 py-1.5 text-xs transition-colors",
                  isOn
                    ? "bg-accent/15 text-fg border border-accent/30"
                    : "text-fg-muted hover:bg-bg hover:text-fg border border-transparent",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70",
                )}
              >
                <span className="min-w-0 text-left">
                  <span className="block truncate">{renderLabel(e.value)}</span>
                  {renderHint?.(e.value) && (
                    <span className="mt-0.5 block line-clamp-2 text-[10px] leading-3 text-fg-dim">
                      {renderHint(e.value)}
                    </span>
                  )}
                </span>
                <span className="text-fg-dim ml-2 shrink-0">{e.count}</span>
              </button>
            </li>
          );
        })}
      </ul>
      {entries.length > max && (
        <button
          onClick={() => setExpanded((x) => !x)}
          className="mt-1 inline-flex min-h-8 items-center rounded px-2 text-[10px] text-fg-dim hover:bg-bg hover:text-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70"
        >
          {expanded ? "Show less" : `Show ${entries.length - max} more`}
        </button>
      )}
    </div>
  );
}
