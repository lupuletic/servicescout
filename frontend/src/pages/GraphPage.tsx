import { useEffect, useMemo, useState } from "react";
import Graph from "graphology";
import useSWR from "swr";
import { SigmaContainer, useLoadGraph, useRegisterEvents, useSigma } from "@react-sigma/core";
import { NodeCircleProgram, EdgeArrowProgram } from "sigma/rendering";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { type EntityRecord, type GraphPayload } from "@/lib/api";
import { Button, Input, KindBadge, PageHeader } from "@/components/ui";

// Sigma 3.x requires the programs to be registered explicitly. Pass these in
// `settings` on the SigmaContainer (single source of truth — do NOT also pass
// `graph={...}` as a prop, or the double-mount race in React StrictMode wipes
// `nodePrograms` mid-flight: see sim51/react-sigma#65.
const SIGMA_SETTINGS = {
  nodeProgramClasses: { circle: NodeCircleProgram },
  edgeProgramClasses: { arrow: EdgeArrowProgram },
  defaultNodeType: "circle",
  defaultEdgeType: "arrow",
  labelColor: { color: "#e2e8f0" },
  labelSize: 11,
  labelFont: "ui-sans-serif, system-ui, sans-serif",
  labelWeight: "500",
  // Adaptive label visibility: a node has to be at least 6px rendered before
  // its label appears. As you zoom in, more labels reveal themselves.
  labelRenderedSizeThreshold: 6,
  // Density: cap how many labels can show simultaneously to keep the canvas
  // legible at any zoom level. Sigma picks the largest-by-size first.
  labelDensity: 0.6,
  labelGridCellSize: 80,
  renderEdgeLabels: false,
  defaultEdgeColor: "#2a3242",
  minCameraRatio: 0.05,
  maxCameraRatio: 4,
  allowInvalidContainer: true,
};

const KIND_COLOR: Record<string, string> = {
  Component: "#3b82f6",
  API: "#eab308",
  Resource: "#a855f7",
  Provider: "#ec4899",
  System: "#10b981",
  Domain: "#06b6d4",
  Group: "#f97316",
};

const ALL_KINDS = ["Component", "API", "Resource", "Provider", "System", "Domain", "Group"];

const FLOW_EDGE_TYPES: Array<{ value: string; label: string }> = [
  { value: "communicatesWith", label: "communicatesWith" },
];

const EVIDENCE_EDGE_TYPES: Array<{ value: string; label: string }> = [
  { value: "consumesApi", label: "consumesApi" },
  { value: "providesApi", label: "providesApi" },
  { value: "dependsOn", label: "dependsOn" },
  { value: "consumesMessage", label: "consumesMessage" },
  { value: "producesMessage", label: "producesMessage" },
  { value: "readsResource", label: "readsResource" },
  { value: "writesResource", label: "writesResource" },
  { value: "ownedBy", label: "ownedBy" },
  { value: "partOf", label: "partOf" },
  { value: "subcomponentOf", label: "subcomponentOf" },
];

function GraphLoader({
  graph,
  onSelect,
  onSigmaReady,
}: {
  graph: Graph;
  onSelect: (ref: string | null) => void;
  onSigmaReady?: (sigma: ReturnType<typeof useSigma>) => void;
}) {
  const loadGraph = useLoadGraph();
  const sigma = useSigma();
  const registerEvents = useRegisterEvents();
  const [hovered, setHovered] = useState<string | null>(null);

  useEffect(() => {
    // Pre-compute the layout synchronously so the user sees the final state
    // on first paint — no FA2 worker, no per-tick refresh, no visible jump.
    // 200 iterations is enough for ~400 nodes; it costs ~500ms blocking on
    // a modern laptop, vs. ~3s of laggy animation with the worker.
    forceAtlas2.assign(graph, {
      iterations: 200,
      settings: {
        gravity: 0.3,
        scalingRatio: 32,
        slowDown: 8,
        strongGravityMode: false,
        barnesHutOptimize: true,
        adjustSizes: true,
        outboundAttractionDistribution: false,
        linLogMode: true,
      },
    });
    loadGraph(graph);
    sigma.getCamera().animatedReset({ duration: 300 });
  }, [graph, loadGraph, sigma]);

  // Notify the parent so it can wire camera-control buttons.
  useEffect(() => {
    onSigmaReady?.(sigma);
  }, [sigma, onSigmaReady]);

  useEffect(() => {
    registerEvents({
      clickNode: ({ node }) => {
        onSelect(node);
        const pos = sigma.getNodeDisplayData(node);
        if (pos) {
          sigma.getCamera().animate({ x: pos.x, y: pos.y, ratio: 0.2 }, { duration: 400 });
        }
      },
      clickStage: () => onSelect(null),
      enterNode: ({ node }) => setHovered(node),
      leaveNode: () => setHovered(null),
    });
  }, [registerEvents, sigma, onSelect]);

  useEffect(() => {
    sigma.setSetting("nodeReducer", (node, data) => {
      if (!hovered) return data;
      const isHovered = node === hovered;
      const isNeighbor = graph.areNeighbors(node, hovered);
      return {
        ...data,
        size: isHovered ? (data.size || 5) * 1.4 : data.size,
        color: isHovered || isNeighbor ? data.color : "#1d212b",
        // Highlight the neighbour LABELS too, not just the nodes —
        // makes the local neighbourhood instantly readable.
        label: isHovered || isNeighbor ? data.label : "",
        forceLabel: isHovered || isNeighbor,
      };
    });
    sigma.setSetting("edgeReducer", (edge, data) => {
      if (!hovered) return data;
      const [s, t] = graph.extremities(edge);
      const touch = s === hovered || t === hovered;
      return { ...data, color: touch ? data.color : "#1d212b", hidden: !touch };
    });
    sigma.refresh();
  }, [hovered, sigma, graph]);

  return null;
}

export function GraphPage() {
  const [kinds, setKinds] = useState<Set<string>>(new Set(["Component", "Provider", "Resource"]));
  const [edgeTypes, setEdgeTypes] = useState<Set<string>>(new Set(["communicatesWith"]));
  const [limit, setLimit] = useState(400);
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [sigmaRef, setSigmaRef] = useState<ReturnType<typeof useSigma> | null>(null);

  const zoom = (factor: number) => {
    if (!sigmaRef) return;
    const cam = sigmaRef.getCamera();
    const cur = cam.getState();
    cam.animate({ ...cur, ratio: Math.max(0.05, Math.min(4, cur.ratio * factor)) }, { duration: 250 });
  };
  const fitToView = () => {
    if (!sigmaRef) return;
    sigmaRef.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1.05, angle: 0 }, { duration: 350 });
  };
  const centerSelected = () => {
    if (!sigmaRef || !selectedRef) return;
    const pos = sigmaRef.getNodeDisplayData(selectedRef);
    if (pos) {
      sigmaRef.getCamera().animate({ x: pos.x, y: pos.y, ratio: 0.2 }, { duration: 350 });
    }
  };

  const graphQs = useMemo(() => {
    const qs = new URLSearchParams();
    Array.from(kinds).sort().forEach((k) => qs.append("kind", k));
    Array.from(edgeTypes).sort().forEach((t) => qs.append("edge_type", t));
    qs.set("limit", String(limit));
    return qs.toString();
  }, [kinds, edgeTypes, limit]);
  const { data, isLoading } = useSWR<GraphPayload>(`/api/graph?${graphQs}`);
  const { data: selectedEntity } = useSWR<EntityRecord & { metadata: any; spec: any; evidence: any[] }>(
    selectedRef ? `/api/entity/${encodeURIComponent(selectedRef)}` : null,
  );

  const graphInstance = useMemo(() => {
    const g = new Graph({ multi: false, type: "directed" });
    if (!data) return g;
    // ----- inbound-degree map for size scaling -----
    const inDegree: Record<string, number> = {};
    const outDegree: Record<string, number> = {};
    data.edges.forEach((e) => {
      inDegree[e.target] = (inDegree[e.target] || 0) + 1;
      outDegree[e.source] = (outDegree[e.source] || 0) + 1;
    });
    // Each system gets a stable angle on a circle (radius 45 so FA2 has room
    // to breathe rather than fighting the gravity well). Nodes are jittered
    // around that anchor with a deterministic per-id hash so React StrictMode
    // double-mounts produce identical seeds.
    const systems = Array.from(new Set(data.nodes.map((n) => n.system || "_"))).sort();
    const systemAngle: Record<string, number> = {};
    systems.forEach((s, i) => { systemAngle[s] = (2 * Math.PI * i) / systems.length; });
    const ringRadius = 45;
    const seed = (id: string, salt: number) => {
      let h = salt;
      for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
      return ((h >>> 0) / 0xffffffff) - 0.5;
    };
    data.nodes.forEach((n) => {
      const deg = (inDegree[n.id] || 0) + (outDegree[n.id] || 0);
      const sysBoost = (n.system && n.system !== "") ? 1.2 : 1.0;
      const ang = systemAngle[n.system || "_"];
      const r = ringRadius + seed(n.id, 1) * 8;
      const a = ang + seed(n.id, 2) * 0.5;
      g.addNode(n.id, {
        label: n.label,
        kind: n.kind,
        system: n.system || "",
        color: KIND_COLOR[n.kind] || "#666",
        size: Math.min(16, (3 + Math.sqrt(deg) * 1.4) * sysBoost),
        x: r * Math.cos(a),
        y: r * Math.sin(a),
        type: "circle",
      });
    });
    data.edges.forEach((e) => {
      if (g.hasNode(e.source) && g.hasNode(e.target) && !g.hasEdge(e.source, e.target)) {
        g.addEdge(e.source, e.target, {
          color: e.type === "communicatesWith" ? "#14b8a6" : "#2a3242",
          type: "arrow",
          size: e.type === "communicatesWith" ? 1.1 : 0.8,
          relationType: e.type,
          endpoint: e.properties?.endpoint || "",
          transport: e.properties?.transport || "",
        });
      }
    });
    return g;
  }, [data]);

  const toggle = (kind: string) => {
    setKinds((prev) => {
      const next = new Set(prev);
      next.has(kind) ? next.delete(kind) : next.add(kind);
      return next;
    });
  };

  const toggleEdgeType = (t: string) => {
    setEdgeTypes((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });
  };

  const showFlowView = () => setEdgeTypes(new Set(["communicatesWith"]));
  const showEvidenceView = () => setEdgeTypes(new Set());
  const flowActive = edgeTypes.size === 1 && edgeTypes.has("communicatesWith");

  const filteredSearch = useMemo(() => {
    if (!data || !search.trim()) return [];
    const q = search.toLowerCase();
    return data.nodes.filter((n) => n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q)).slice(0, 8);
  }, [data, search]);

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Graph"
        description={
          data
            ? `Showing ${data.nodes.length} / ${data.node_total} nodes · ${data.edges.length} edges. Force-directed layout.`
            : "Loading…"
        }
        actions={
          <>
            <Input
              placeholder="Find a node…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="w-56"
            />
            <Button
              variant="outline"
              onClick={() => setLimit((l) => (l === 400 ? 1500 : 400))}
              title="Toggle node limit"
            >
              {limit === 400 ? "Show all" : "Top 400"}
            </Button>
          </>
        }
      />
      <div className="grid grid-cols-[1fr_320px] min-h-0">
        <div className="relative bg-bg">
          {/* Kind filter row */}
          <div className="absolute top-3 left-3 z-10 flex flex-wrap gap-1.5 max-w-[70%]">
            {ALL_KINDS.map((kind) => (
              <button
                key={kind}
                onClick={() => toggle(kind)}
                className={`flex items-center gap-1.5 rounded border px-2 py-1 text-xs transition-colors ${
                  kinds.has(kind)
                    ? "border-border-strong bg-bg-elevated"
                    : "border-border bg-bg/40 opacity-40 hover:opacity-70"
                }`}
              >
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: KIND_COLOR[kind] }}
                />
                <span className="text-fg-muted">{kind}</span>
              </button>
            ))}
          </div>

          <div className="absolute top-12 left-3 z-10 flex flex-col gap-1.5 max-w-[72%]">
            <div className="flex flex-wrap items-center gap-1">
              <button
                onClick={showFlowView}
                className={`rounded border px-2 py-0.5 text-[10px] transition-colors ${
                  flowActive
                    ? "border-accent/50 bg-accent/15 text-fg"
                    : "border-border bg-bg/40 text-fg-muted hover:text-fg"
                }`}
              >
                Flow
              </button>
              <button
                onClick={showEvidenceView}
                className={`rounded border px-2 py-0.5 text-[10px] transition-colors ${
                  !flowActive
                    ? "border-accent/50 bg-accent/15 text-fg"
                    : "border-border bg-bg/40 text-fg-muted hover:text-fg"
                }`}
              >
                Evidence
              </button>
            </div>

            <div className="flex flex-wrap items-center gap-1">
              {FLOW_EDGE_TYPES.map((et) => {
                const on = edgeTypes.has(et.value);
                return (
                  <button
                    key={et.value}
                    onClick={() => setEdgeTypes(new Set([et.value]))}
                    className={`rounded border px-1.5 py-0.5 text-[10px] transition-colors ${
                      on
                        ? "border-teal-400/50 bg-teal-400/15 text-fg"
                        : "border-border bg-bg/40 text-fg-muted hover:text-fg"
                    }`}
                  >
                    {et.label}
                  </button>
                );
              })}
            </div>

            <div className="flex flex-wrap gap-1">
              {EVIDENCE_EDGE_TYPES.map((et) => {
              const on = edgeTypes.has(et.value);
              const anyOn = edgeTypes.size > 0;
              return (
                <button
                  key={et.value}
                  onClick={() => toggleEdgeType(et.value)}
                  className={`rounded border px-1.5 py-0.5 text-[10px] transition-colors ${
                    on
                      ? "border-accent/40 bg-accent/15 text-fg"
                      : anyOn
                        ? "border-border bg-bg/40 opacity-50 text-fg-muted"
                        : "border-border bg-bg/40 text-fg-muted hover:text-fg"
                  }`}
                >
                  {et.label}
                </button>
              );
            })}
            {edgeTypes.size > 0 && (
              <button
                onClick={() => setEdgeTypes(new Set())}
                className="rounded border border-border bg-bg/40 px-1.5 py-0.5 text-[10px] text-fg-dim hover:text-accent"
              >
                all evidence
              </button>
            )}
            </div>
          </div>

          {/* Search results overlay */}
          {filteredSearch.length > 0 && (
            <div className="absolute top-3 right-3 z-10 w-72 rounded-md border border-border bg-bg-elevated shadow-lg max-h-72 overflow-auto">
              {filteredSearch.map((n) => (
                <button
                  key={n.id}
                  onClick={() => {
                    setSelectedRef(n.id);
                    setSearch("");
                  }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-bg"
                >
                  <KindBadge kind={n.kind} />
                  <span className="truncate text-fg">{n.label}</span>
                </button>
              ))}
            </div>
          )}

          {isLoading && (
            <div className="absolute inset-0 flex items-center justify-center text-fg-muted">
              Building force-directed layout…
            </div>
          )}

          {/* Camera control bar — bottom-right */}
          <div className="absolute bottom-3 right-3 z-10 flex flex-col gap-1.5 rounded-md border border-border bg-bg-elevated/80 backdrop-blur-sm p-1">
            <button
              onClick={() => zoom(0.75)}
              title="Zoom in"
              className="h-7 w-7 rounded grid place-items-center text-fg-muted hover:text-fg hover:bg-bg"
            >
              +
            </button>
            <button
              onClick={() => zoom(1.4)}
              title="Zoom out"
              className="h-7 w-7 rounded grid place-items-center text-fg-muted hover:text-fg hover:bg-bg"
            >
              −
            </button>
            <button
              onClick={fitToView}
              title="Fit to view"
              className="h-7 w-7 rounded grid place-items-center text-fg-muted hover:text-fg hover:bg-bg text-[11px]"
            >
              ⛶
            </button>
            {selectedRef && (
              <button
                onClick={centerSelected}
                title="Centre on selected"
                className="h-7 w-7 rounded grid place-items-center text-accent hover:text-fg hover:bg-bg text-[11px]"
              >
                ◎
              </button>
            )}
          </div>

          <SigmaContainer
            className="sigma-container"
            settings={SIGMA_SETTINGS}
          >
            <GraphLoader graph={graphInstance} onSelect={setSelectedRef} onSigmaReady={setSigmaRef} />
          </SigmaContainer>
        </div>

        {/* Detail panel */}
        <aside className="border-l border-border bg-bg-elevated overflow-auto">
          {!selectedRef && (
            <div className="p-6 text-sm text-fg-muted">
              <h3 className="text-base font-semibold text-fg mb-2">Select a node</h3>
              <p>
                Click any node in the graph to inspect it — tagline, kind, source repo, dependencies. Hover to
                highlight the local neighbourhood.
              </p>
              <p className="mt-4 text-xs text-fg-dim">
                Tip: hide kinds you don't care about with the filter chips top-left to declutter the canvas.
              </p>
            </div>
          )}
          {selectedRef && selectedEntity && (
            <EntityDetail entity={selectedEntity as any} />
          )}
          {selectedRef && !selectedEntity && (
            <div className="p-6 text-sm text-fg-muted">Loading {selectedRef}…</div>
          )}
        </aside>
      </div>
    </div>
  );
}

function EntityDetail({
  entity,
}: {
  entity: EntityRecord & { metadata: any; spec: any; evidence: any[] };
}) {
  const annotations = entity.metadata?.annotations || {};
  const sourceRepos: string[] = annotations.source_repos || [];
  const aliases: string[] = annotations.aliases || [];
  const environments: string[] = entity.spec?.environments || [];
  return (
    <div className="p-6 space-y-4 text-sm">
      <div>
        <KindBadge kind={entity.kind} />
        <h2 className="mt-2 text-lg font-semibold text-fg">{entity.name}</h2>
        {annotations.tagline && (
          <p className="text-sm text-fg-muted mt-1">{annotations.tagline}</p>
        )}
      </div>

      {sourceRepos.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Repos</div>
          {sourceRepos.map((r) => (
            <a
              key={r}
              href={`https://github.com/${r}`}
              target="_blank"
              rel="noreferrer"
              className="block text-accent hover:underline truncate"
            >
              {r}
            </a>
          ))}
        </div>
      )}

      {environments.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Environments</div>
          <div className="flex flex-wrap gap-1">
            {environments.map((e) => (
              <span key={e} className="rounded border border-border bg-bg px-1.5 py-0.5 text-xs text-fg-muted">
                {e}
              </span>
            ))}
          </div>
        </div>
      )}

      {entity.spec?.type && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Type</div>
          <div className="text-fg-muted">{entity.spec.type}</div>
        </div>
      )}

      {aliases.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Aliases</div>
          <div className="flex flex-wrap gap-1">
            {aliases.slice(0, 8).map((a) => (
              <span key={a} className="rounded border border-border bg-bg px-1.5 py-0.5 text-xs text-fg-muted">
                {a}
              </span>
            ))}
            {aliases.length > 8 && <span className="text-xs text-fg-dim">+{aliases.length - 8}</span>}
          </div>
        </div>
      )}

      {entity.evidence?.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Evidence</div>
          <ul className="space-y-1 text-xs text-fg-muted">
            {entity.evidence.slice(0, 5).map((e: any, i: number) => (
              <li key={i} className="font-mono truncate">
                {e.path}:{e.line}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
