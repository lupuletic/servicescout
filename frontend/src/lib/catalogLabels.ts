export const KIND_META: Record<string, { label: string; plural: string; description: string; color: string }> = {
  Component: {
    label: "Service",
    plural: "Services",
    description: "Deployable/runnable code: services, workers, jobs, frontends, CLIs, functions.",
    color: "#3b82f6",
  },
  API: {
    label: "API contract",
    plural: "API contracts",
    description: "Named interfaces a service exposes, with routes or operations attached.",
    color: "#eab308",
  },
  Resource: {
    label: "Data or queue",
    plural: "Data & queues",
    description: "Databases, queues, topics, streams, buckets, caches, indexes, config stores.",
    color: "#a855f7",
  },
  Provider: {
    label: "External provider",
    plural: "External providers",
    description: "Runtime third-party SaaS or cloud services such as Stripe, GCS, Datadog.",
    color: "#ec4899",
  },
  System: {
    label: "System",
    plural: "Systems",
    description: "A product or platform area grouping related services and APIs.",
    color: "#10b981",
  },
  Domain: {
    label: "Domain",
    plural: "Domains",
    description: "Business capability area, used to group systems at a higher level.",
    color: "#06b6d4",
  },
  Group: {
    label: "Owning team",
    plural: "Owning teams",
    description: "Team or org ownership group used for accountability.",
    color: "#f97316",
  },
};

export const EDGE_TYPE_META: Record<string, { label: string; description: string }> = {
  communicatesWith: {
    label: "Service flow",
    description: "Collapsed service-to-service communication inferred from lower-level evidence.",
  },
  consumesApi: {
    label: "Calls API",
    description: "A service calls another service's API contract.",
  },
  providesApi: {
    label: "Exposes API",
    description: "A service exposes an API contract.",
  },
  dependsOn: {
    label: "Depends on",
    description: "A general runtime dependency.",
  },
  consumesMessage: {
    label: "Consumes messages",
    description: "A service reads messages from a queue, topic, stream, or subscription.",
  },
  producesMessage: {
    label: "Publishes messages",
    description: "A service writes messages to a queue, topic, stream, or subscription.",
  },
  readsResource: {
    label: "Reads data",
    description: "A service reads from a data/resource endpoint.",
  },
  writesResource: {
    label: "Writes data",
    description: "A service writes to a data/resource endpoint.",
  },
  ownedBy: {
    label: "Owned by",
    description: "Ownership relationship to a team/group.",
  },
  partOf: {
    label: "Part of",
    description: "Membership in a system or domain.",
  },
  subcomponentOf: {
    label: "Subcomponent",
    description: "A deployable is nested under another component.",
  },
};

export const kindLabel = (kind: string, plural = false) => {
  const meta = KIND_META[kind];
  if (!meta) return kind;
  return plural ? meta.plural : meta.label;
};

export const kindColor = (kind: string) => KIND_META[kind]?.color || "#64748b";

export const edgeTypeLabel = (type: string) => EDGE_TYPE_META[type]?.label || type;
