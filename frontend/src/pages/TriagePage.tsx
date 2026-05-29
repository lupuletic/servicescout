import { useState, useMemo } from "react";
import useSWR, { useSWRConfig } from "swr";
import { CheckCircle2 } from "lucide-react";
import { Button, EmptyState, Input, PageHeader, Select } from "@/components/ui";

type TriageRow = {
  ref: string;
  name: string;
  aliases: string[];
  inbound: number;
  evidence: Array<{ path: string; line: number; snippet: string }>;
};

type TriageResponse = { count: number; components: TriageRow[] };
type DecisionLogResponse = {
  decisions: Array<{ type?: string; entity: string; action: string; owner?: string; due_date?: string; reviewer?: string; reason?: string; ts?: string; category?: string; repo?: string; into?: string }>;
  total: number;
};
type FactRow = {
  id: string;
  repo: string;
  category: string;
  label: string;
  combined_verdict: "disconfirmed" | "mixed";
  explanation: string;
  phase_a_verdict?: string;
  phase_b_verdict?: string;
  confidence?: string;
  reasons: string[];
  evidence: Array<{ path: string; line: number; snippet?: string }>;
  owner?: string;
  due_date?: string;
  last_action?: string;
  current_fact: Record<string, unknown>;
};
type FactsResponse = { count: number; assigned: number; unassigned: number; facts: FactRow[] };

type EntityRecord = {
  ref: string;
  kind: string;
  name: string;
};

const CATEGORIES = ["saas", "cloud", "payment", "identity", "analytics", "messaging", "observability", "other"];

export function TriagePage() {
  const { data, isLoading } = useSWR<TriageResponse>("/api/triage.json");
  const { data: factsData, isLoading: factsLoading } = useSWR<FactsResponse>("/api/triage/facts");
  const { data: decisions } = useSWR<DecisionLogResponse>("/api/triage/decisions");
  const { data: allEntities } = useSWR<{ entities: EntityRecord[] }>("/api/entities?kind=Component&limit=2000");
  const { mutate } = useSWRConfig();
  const [mode, setMode] = useState<"facts" | "components">("facts");
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [selectedFactId, setSelectedFactId] = useState<string | null>(null);

  const rows = useMemo(() => data?.components ?? [], [data?.components]);
  const selected = useMemo(() => rows.find((r) => r.ref === selectedRef) ?? null, [rows, selectedRef]);
  const facts = useMemo(() => factsData?.facts ?? [], [factsData?.facts]);
  const selectedFact = useMemo(() => facts.find((f) => f.id === selectedFactId) ?? null, [facts, selectedFactId]);

  const submit = async (form: FormData) => {
    const r = await fetch("/triage/decide", { method: "POST", body: form, redirect: "manual" });
    // FastAPI returns a 303 redirect; manual mode means we treat it as success.
    if (r.ok || r.status === 0 || r.status === 303) {
      mutate("/api/triage.json");
      mutate("/api/triage/decisions");
      setSelectedRef(null);
    } else {
      alert(`Decision failed: ${r.status} ${r.statusText}`);
    }
  };

  const submitFact = async (form: FormData) => {
    const r = await fetch("/api/triage/facts/decide", { method: "POST", body: form });
    if (r.ok) {
      mutate("/api/triage/facts");
      mutate("/api/triage/decisions");
      setSelectedFactId(null);
    } else {
      alert(`Decision failed: ${r.status} ${r.statusText}`);
    }
  };

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader
        title="Triage"
        description={
          factsData && data
            ? `${factsData.count} verifier facts · ${data.count} external components`
            : "Loading…"
        }
      />
      <div className="grid min-h-0 grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-h-0 grid grid-rows-[auto_1fr]">
          <div className="flex items-center gap-1 overflow-x-auto border-b border-border bg-bg-elevated/40 px-4 py-2 sm:px-6">
            <TabButton active={mode === "facts"} onClick={() => { setMode("facts"); setSelectedRef(null); }}>
              Disconfirmed facts
              {" "}
              <span className="ml-1 text-fg-dim">{factsData?.count ?? "-"}</span>
            </TabButton>
            <TabButton active={mode === "components"} onClick={() => { setMode("components"); setSelectedFactId(null); }}>
              External components
              {" "}
              <span className="ml-1 text-fg-dim">{data?.count ?? "-"}</span>
            </TabButton>
          </div>
          <div className="overflow-auto">
          {mode === "facts" && (
            <>
              {factsLoading && <div className="p-6 text-fg-muted">Loading facts...</div>}
              {!factsLoading && facts.length === 0 && (
                <div className="p-6">
                  <EmptyState
                    icon={CheckCircle2}
                    title="No disconfirmed facts"
                    description="Verifier facts that need operator review will appear here."
                  />
                </div>
              )}
              {facts.length > 0 && (
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-bg-elevated text-xs uppercase tracking-wider text-fg-dim">
                    <tr>
                      <th className="text-left px-6 py-2.5 font-medium">Fact</th>
                      <th className="text-left px-3 py-2.5 font-medium">Repo</th>
                      <th className="text-left px-3 py-2.5 font-medium">Verdict</th>
                      <th className="text-left px-3 py-2.5 font-medium">Owner</th>
                    </tr>
                  </thead>
                  <tbody>
                    {facts.map((fact) => (
                      <tr
                        key={fact.id}
                        onClick={() => setSelectedFactId(fact.id)}
                        className={`border-t border-border hover:bg-bg-elevated cursor-pointer ${
                          selectedFactId === fact.id ? "bg-bg-elevated" : ""
                        }`}
                      >
                        <td className="px-6 py-2.5">
                          <div className="font-medium text-fg truncate max-w-xl">{fact.label}</div>
                          <div className="text-xs text-fg-dim">{fact.category}</div>
                        </td>
                        <td className="px-3 py-2.5 font-mono text-xs text-fg-muted">{fact.repo}</td>
                        <td className="px-3 py-2.5 text-fg-muted">{fact.combined_verdict}</td>
                        <td className="px-3 py-2.5 text-fg-muted">{fact.owner || "Unassigned"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
          {mode === "components" && (
            <>
            {isLoading && <div className="p-6 text-fg-muted">Loading…</div>}
            {!isLoading && rows.length === 0 && (
              <div className="p-6">
                <EmptyState
                  icon={CheckCircle2}
                  title="No external components"
                  description="All extracted external components have either been linked, marked, merged, or skipped."
                />
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
            </>
          )}
            </div>
        </div>

        {/* Decision panel */}
        <aside className="overflow-auto border-t border-border bg-bg-elevated xl:border-l xl:border-t-0">
          {mode === "facts" && selectedFact && (
            <FactDecisionPanel row={selectedFact} onSubmit={submitFact} onCancel={() => setSelectedFactId(null)} />
          )}
          {mode === "components" && selected && (
            <DecisionPanel
              row={selected}
              realComponents={allEntities?.entities ?? []}
              onSubmit={submit}
              onCancel={() => setSelectedRef(null)}
            />
          )}
          {((mode === "facts" && !selectedFact) || (mode === "components" && !selected)) && (
            <div className="p-6 text-sm text-fg-muted">
              <h3 className="text-base font-semibold text-fg mb-2">Pick a row</h3>
              <p>{decisions?.total ?? 0} recorded decisions.</p>
              {(decisions?.decisions || []).length > 0 && (
                <div className="mt-5 space-y-2">
                  <div className="text-xs uppercase tracking-wider text-fg-dim">Decision log</div>
                  {(decisions?.decisions || []).slice(0, 8).map((decision) => (
                    <div key={`${decision.ts}-${decision.entity}`} className="rounded border border-border bg-bg p-3">
                      <div className="font-mono text-xs text-fg truncate">{decision.entity}</div>
                      <div className="mt-1 flex items-center justify-between gap-2 text-xs">
                        <span className="text-fg-muted">{decision.action.replace("_", " ")}</span>
                        <span className="text-fg-dim">{decision.ts ? new Date(decision.ts).toLocaleDateString() : ""}</span>
                      </div>
                      {decision.reason && <div className="mt-1 text-xs text-fg-dim">{decision.reason}</div>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}

function TabButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`shrink-0 rounded-md border px-3 py-1.5 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 ${
        active
          ? "border-accent/40 bg-accent/15 text-fg"
          : "border-transparent text-fg-muted hover:bg-bg hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}

function FactDecisionPanel({
  row,
  onSubmit,
  onCancel,
}: {
  row: FactRow;
  onSubmit: (form: FormData) => void;
  onCancel: () => void;
}) {
  const [action, setAction] = useState<"assign_owner" | "mark_corrected" | "accept_risk" | "false_positive">(
    row.owner ? "mark_corrected" : "assign_owner",
  );
  const [owner, setOwner] = useState(row.owner || "");
  const [dueDate, setDueDate] = useState(row.due_date || "");
  const [reviewer, setReviewer] = useState("");
  const [reason, setReason] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const form = new FormData();
    form.set("fact_id", row.id);
    form.set("action", action);
    form.set("owner", owner);
    form.set("due_date", dueDate);
    form.set("reviewer", reviewer);
    form.set("reason", reason);
    onSubmit(form);
  };

  return (
    <form onSubmit={handleSubmit} className="p-6 space-y-4 text-sm">
      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1">Verifier fact</div>
        <h2 className="text-lg font-semibold text-fg">{row.label}</h2>
        <code className="text-xs text-fg-dim font-mono break-all">{row.id}</code>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <MiniFact label="Verdict" value={row.combined_verdict} />
        <MiniFact label="Confidence" value={row.confidence || "n/a"} />
        <MiniFact label="Phase A" value={row.phase_a_verdict || "n/a"} />
        <MiniFact label="Phase B" value={row.phase_b_verdict || "n/a"} />
      </div>

      {row.reasons.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Verifier reasons</div>
          <ul className="space-y-1 text-xs text-fg-muted">
            {row.reasons.map((reasonText) => (
              <li key={reasonText} className="rounded border border-border bg-bg px-2 py-1">
                {reasonText}
              </li>
            ))}
          </ul>
        </div>
      )}

      {row.evidence.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Evidence</div>
          <ul className="space-y-2 text-xs text-fg-muted">
            {row.evidence.slice(0, 4).map((e, i) => (
              <li key={`${e.path}:${e.line}:${i}`} className="rounded border border-border bg-bg p-2">
                <div className="font-mono text-fg">{e.path}:{e.line}</div>
                {e.snippet && <div className="mt-1 font-mono text-fg-dim">{e.snippet}</div>}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="space-y-2">
        <div className="text-xs uppercase tracking-wider text-fg-dim">Ritual</div>
        <div className="grid grid-cols-2 gap-2">
          {(["assign_owner", "mark_corrected", "accept_risk", "false_positive"] as const).map((a) => (
            <button
              key={a}
              type="button"
              onClick={() => setAction(a)}
              className={`rounded-md border px-3 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 ${
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

      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Owner</div>
        <Input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="team or person" />
      </div>

      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Due date</div>
        <Input type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} />
      </div>

      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Reviewer</div>
        <Input value={reviewer} onChange={(e) => setReviewer(e.target.value)} placeholder="you@org.com" />
      </div>

      <div>
        <div className="text-xs uppercase tracking-wider text-fg-dim mb-1.5">Decision note</div>
        <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="what changed or why this is accepted" />
      </div>

      <details className="rounded border border-border bg-bg p-3">
        <summary className="cursor-pointer text-xs uppercase tracking-wider text-fg-dim">Raw fact</summary>
        <pre className="mt-2 max-h-56 overflow-auto text-xs text-fg-muted whitespace-pre-wrap">
          {JSON.stringify(row.current_fact, null, 2)}
        </pre>
      </details>

      <div className="flex gap-2 pt-2">
        <Button type="submit">Record decision</Button>
        <Button type="button" variant="ghost" onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

function MiniFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded border border-border bg-bg p-2">
      <div className="text-[10px] uppercase tracking-wider text-fg-dim">{label}</div>
      <div className="mt-1 text-xs font-medium text-fg">{value}</div>
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
              className={`rounded-md border px-3 py-2 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/70 ${
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
          <Select
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>{c}</option>
            ))}
          </Select>
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
          <Select
            value={into}
            onChange={(e) => setInto(e.target.value)}
            required
          >
            <option value="">Pick a component…</option>
            {realComponents.map((c) => (
              <option key={c.ref} value={c.ref}>{c.name}</option>
            ))}
          </Select>
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
