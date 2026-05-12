import { useEffect, useMemo } from "react";
import Graph from "graphology";
import useSWR from "swr";
import { SigmaContainer, useLoadGraph, useRegisterEvents } from "@react-sigma/core";
import { NodeCircleProgram, EdgeArrowProgram } from "sigma/rendering";
import ForceAtlas2Layout from "graphology-layout-forceatlas2/worker";
import { useNavigate } from "react-router-dom";
import { type GraphPayload } from "@/lib/api";

const KIND_COLOR: Record<string, string> = {
  Component: "#3b82f6",
  API: "#eab308",
  Resource: "#a855f7",
  Provider: "#ec4899",
  System: "#10b981",
  Domain: "#06b6d4",
  Group: "#f97316",
};

const MINI_SETTINGS = {
  nodeProgramClasses: { circle: NodeCircleProgram },
  edgeProgramClasses: { arrow: EdgeArrowProgram },
  defaultNodeType: "circle",
  defaultEdgeType: "arrow",
  labelColor: { color: "#cbd5e1" },
  labelSize: 10,
  labelFont: "ui-sans-serif, system-ui, sans-serif",
  labelWeight: "500",
  labelRenderedSizeThreshold: 4,
  renderEdgeLabels: false,
  defaultEdgeColor: "#2a3242",
  minCameraRatio: 0.05,
  maxCameraRatio: 4,
  allowInvalidContainer: true,
};

function MiniLoader({ graph }: { graph: Graph }) {
  const loadGraph = useLoadGraph();
  useEffect(() => {
    loadGraph(graph);
    const layout = new ForceAtlas2Layout(graph, {
      settings: { gravity: 1, scalingRatio: 8, slowDown: 3, barnesHutOptimize: true, adjustSizes: true },
    });
    layout.start();
    setTimeout(() => layout.stop(), 2500);
    return () => layout.kill();
  }, [graph, loadGraph]);
  return null;
}

/** Embedded depth-1 graph card for the EntityPage Overview tab. */
export function MiniGraph({ centerRef, depth = 1, height = 360 }: { centerRef: string; depth?: number; height?: number }) {
  const { data, isLoading } = useSWR<GraphPayload & { center?: string }>(
    `/api/graph?center=${encodeURIComponent(centerRef)}&depth=${depth}`,
  );
  const navigate = useNavigate();

  const graphInstance = useMemo(() => {
    const g = new Graph({ multi: false, type: "directed" });
    if (!data) return g;
    data.nodes.forEach((n) => {
      const isCenter = (n as any).is_center === true;
      g.addNode(n.id, {
        label: n.label,
        kind: n.kind,
        color: KIND_COLOR[n.kind] || "#666",
        size: isCenter ? 12 : 5 + Math.min(6, Math.sqrt((n as any).degree || 1)),
        x: Math.random(),
        y: Math.random(),
        type: "circle",
      });
    });
    data.edges.forEach((e) => {
      if (g.hasNode(e.source) && g.hasNode(e.target) && !g.hasEdge(e.source, e.target)) {
        g.addEdge(e.source, e.target, { color: "#2a3242", type: "arrow", size: 0.7 });
      }
    });
    return g;
  }, [data]);

  return (
    <div
      className="relative rounded-lg border border-border bg-bg overflow-hidden"
      style={{ height }}
    >
      {isLoading && (
        <div className="absolute inset-0 flex items-center justify-center text-fg-dim text-xs">
          Loading neighbourhood…
        </div>
      )}
      {data && data.nodes.length <= 1 && (
        <div className="absolute inset-0 flex items-center justify-center text-fg-dim text-xs">
          No connected entities.
        </div>
      )}
      <SigmaContainer
        className="sigma-container"
        settings={MINI_SETTINGS}
        style={{ height: "100%", width: "100%" }}
      >
        <MiniLoader graph={graphInstance} />
        <MiniInteractions onNodeClick={(ref) => navigate(`/entity/${encodeURIComponent(ref)}`)} />
      </SigmaContainer>
    </div>
  );
}

function MiniInteractions({ onNodeClick }: { onNodeClick: (ref: string) => void }) {
  const registerEvents = useRegisterEvents();
  useEffect(() => {
    registerEvents({ clickNode: ({ node }) => onNodeClick(node) });
  }, [registerEvents, onNodeClick]);
  return null;
}
