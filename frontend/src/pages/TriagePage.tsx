import { useState, useMemo } from "react";
import useSWR, { useSWRConfig } from "swr";
import { Button, Input, PageHeader } from "@/components/ui";

type TriageRow = {
  ref: string;
  name: string;
  aliases: string[];
  inbound: number;
  evidence: Array<{ path: string; line: number; snippet: string }>;
};

type TriageResponse = { count: number; components: TriageRow[] };

type EntityRecord = {
  ref: string;
  kind: string;
  name: string;
};

const CATEGORIES = ["saas", "cloud", "payment", "identity", "analytics", "messaging", "observability", "other"];

export function TriagePage() {
  const { data, isLoading } = useSWR<TriageResponse>("/api/triage.json");
  const { data: allEntities } = useSWR<{ entities: EntityRecord[] }>("/api/entities?kind=Component&limit=2000");
  const { mutate } = useSWRConfig();
  const [selectedRef, setSelectedRef] = useState<string | null>(null);

  const rows = data?.components ?? [];
  const selected = useMemo(() => rows.find((r) => r.ref === selectedRef) ?? null, [rows, selectedRef]);

  const submit = async (form: FormData) => {
    const r = await fetch("/triage/decide", { method: "POST", body: form, redirect: "manual" });
    // FastAPI returns a 303 redirect; manual mode means we treat it as success.
    if (r.ok || r.status === 0 || r.status === 303) {
      mutate("/api/triage.json");
      setSelectedRef(null);
    } else {
      alert(`Decision failed: ${r.status} ${r.statusText}`);
    }
  };

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Triage"
        description={
          data
            ? `${data.count} unresolved external components awaiting decisions.`
            : "Loading…"
        }
      />
      <div className="grid grid-cols-[1fr_380px] min-h-0">
        <div className="overflow-auto">
          {isLoading && <div className="p-6 text-fg-muted">Loading…</div>}
          {!isLoading && rows.length === 0 && (
            <div className="p-6 text-fg-muted">
              Nothing to triage. All external components have been processed.
            </div>
          )}
          {rows.length > 0 && (
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-bg-elevated text-xs uppercase tracking-wider text-fg-dim">
                <tr>
                  <th className="text-left px-6 py-2.5 font-medium">Name</th>
                  <th className="text-left px-3 py-2.5 font-medium">Aliases</th>
                  <th className="text-right px-3 py-2.5 font-medium">Inbound</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr
                    key={row.ref}
                    onClick={() => setSelectedRef(row.ref)}
                    className={`border-t border-border hover:bg-bg-elevated cursor-pointer ${
                      selectedRef === row.ref ? "bg-bg-elevated" : ""
                    }`}
                  >
                    <td className="px-6 py-2.5 font-medium text-fg">{row.name}</td>
                    <td className="px-3 py-2.5 text-fg-muted truncate max-w-xl">
                      {row.aliases.slice(0, 4).join(", ")}
                      {row.aliases.length > 4 && <span className="text-fg-dim"> +{row.aliases.length - 4}</span>}
                    </td>
                    <td className="px-3 py-2.5 text-right text-fg-muted">{row.inbound}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Decision panel */}
        <aside className="border-l border-border bg-bg-elevated overflow-auto">
          {!selected && (
            <div className="p-6 text-sm text-fg-muted">
              <h3 className="text-base font-semibold text-fg mb-2">Pick a row</h3>
              <p>Click a row on the left to inspect it and apply one of four decisions.</p>
            </div>
          )}
          {selected && (
            <DecisionPanel
              row={selected}
              realComponents={allEntities?.entities ?? []}
              onSubmit={submit}
              onCancel={() => setSelectedRef(null)}
            />
          )}
        </aside>
      </div>
    </div>
  );
}

function DecisionPanel({
  row,
  realComponents,
  onSubmit,
  onCancel,
}: {
  row: TriageRow;
  realComponents: EntityRecord[];
  onSubmit: (form: FormData) => void;
  onCancel: () => void;
}) {
  const [action, setAction] = useState<"mark_external" | "link" | "merge" | "skip">("mark_external");
  const [category, setCategory] = useState("other");
  const [repo, setRepo] = useState("");
  const [into, setInto] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [reason, setReason] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const form = new FormData();
    form.set("entity", row.ref);
    form.set("action", action);
    form.set("category", category);
    form.set("repo", repo);
    form.set("into", into);
    form.set("reviewer", reviewer);
    form.set("reason", reason);
    onSubmit(form);
  };

  return (
    <form onSubmit={handleSubmit} className="p-6 space-y-4 text-sm">
      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1">Entity</div>
        <h2 className="text-lg font-semibold text-fg">{row.name}</h2>
        <code className="text-xs text-fg-dim font-mono">{row.ref}</code>
      </div>

      {row.aliases.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Aliases</div>
          <div className="flex flex-wrap gap-1">
            {row.aliases.slice(0, 12).map((a) => (
              <span key={a} className="rounded border border-border bg-bg px-1.5 py-0.5 text-xs text-fg-muted">
                {a}
              </span>
            ))}
          </div>
        </div>
      )}

      {row.evidence.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Evidence</div>
          <ul className="space-y-1 text-xs font-mono text-fg-muted">
            {row.evidence.slice(0, 5).map((e, i) => (
              <li key={i}>{e.path}:{e.line}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-2">
        <div className="text-xs uppercase tracking-wider text-fg-dim">Decision</div>
        <div className="grid grid-cols-2 gap-2">
          {(["mark_external", "link", "merge", "skip"] as const).map((a) => (
            <button
              key={a}
              type="button"
              onClick={() => setAction(a)}
              className={`rounded-md border px-3 py-2 text-xs font-medium transition-colors ${
                action === a
                  ? "border-accent bg-accent/15 text-fg"
                  : "border-border text-fg-muted hover:border-border-strong hover:text-fg"
              }`}
            >
              {a.replace("_", " ")}
            </button>
          ))}
        </div>
      </div>

      {action === "mark_external" && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Category</div>
          <select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="w-full h-9 rounded-md border border-border bg-bg-elevated px-3 text-sm text-fg focus:outline-none focus:ring-1 focus:ring-accent"
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </select>
        </div>
      )}

      {action === "link" && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">GitHub repo (org/name)</div>
          <Input
            placeholder="my-org/my-repo"
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            required
          />
        </div>
      )}

      {action === "merge" && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Merge into</div>
          <select
            value={into}
            onChange={(e) => setInto(e.target.value)}
            required
            className="w-full h-9 rounded-md border border-border bg-bg-elevated px-3 text-sm text-fg focus:outline-none focus:ring-1 focus:ring-accent"
          >
            <option value="">Pick a component…</option>
            {realComponents.map((c) => (
              <option key={c.ref} value={c.ref}>{c.name}</option>
            ))}
          </select>
        </div>
      )}

      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Reviewer (optional)</div>
        <Input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="you@org.com" />
      </div>

      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Reason (optional)</div>
        <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="why this decision" />
      </div>

      <div className="flex gap-2 pt-2">
        <Button type="submit">Apply decision</Button>
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
