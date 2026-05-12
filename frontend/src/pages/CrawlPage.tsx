import useSWR from "swr";
import { type StatusPayload } from "@/lib/api";
import { PageHeader, Stat } from "@/components/ui";

export function CrawlPage() {
  const { data } = useSWR<StatusPayload>("/api/state.json", { refreshInterval: 3000 });

  return (
    <div className="h-full grid grid-rows-[auto_1fr]">
      <PageHeader title="Crawl" description="Live state. Polls every 3 seconds." />
      <div className="overflow-auto p-6 space-y-6 max-w-5xl">
        <div className="grid grid-cols-4 gap-3">
          <Stat label="Entities" value={data?.summary?.entities ?? "—"} />
          <Stat label="Relations" value={data?.summary?.relations ?? "—"} />
          <Stat label="Repos indexed" value={data?.summary?.repos_indexed ?? "—"} />
          <Stat
            label="Cumulative spend"
            value={data?.cumulative_spend_usd != null ? `$${data.cumulative_spend_usd.toFixed(2)}` : "—"}
          />
        </div>

        <div className="grid grid-cols-3 gap-3">
          <Stat
            label="Crawler"
            value={data?.crawler?.running ? "Running" : "Idle"}
            hint={data?.crawler?.running && data.crawler.uptime ? `Up ${data.crawler.uptime}` : undefined}
          />
          <Stat
            label="Backend"
            value={data?.backend || "—"}
            hint="Pluggable storage. Kuzu / JSON."
          />
          <Stat
            label="Unresolved"
            value={data?.summary?.unresolved_external_components ?? "—"}
            hint="External components awaiting triage."
          />
        </div>

        {data?.recent_extractions && data.recent_extractions.length > 0 && (
          <section>
            <h2 className="text-sm uppercase tracking-wider text-fg-dim mb-2">Recent extractions</h2>
            <div className="rounded-lg border border-border bg-bg-elevated overflow-hidden">
              <table className="w-full text-sm">
                <thead className="text-xs uppercase tracking-wider text-fg-dim bg-bg-elevated">
                  <tr>
                    <th className="text-left px-3 py-2 font-medium">Repo</th>
                    <th className="text-left px-3 py-2 font-medium">Model</th>
                    <th className="text-right px-3 py-2 font-medium">Cost</th>
                    <th className="text-right px-3 py-2 font-medium">Duration</th>
                    <th className="text-left px-3 py-2 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_extractions.map((e) => (
                    <tr key={e.repo + e.duration_seconds} className="border-t border-border">
                      <td className="px-3 py-2 font-mono text-xs text-fg">{e.repo}</td>
                      <td className="px-3 py-2 text-fg-muted">{e.model}</td>
                      <td className="px-3 py-2 text-right text-fg-muted">${e.cost.toFixed(4)}</td>
                      <td className="px-3 py-2 text-right text-fg-muted">{Math.round(e.duration_seconds || 0)}s</td>
                      <td className="px-3 py-2 text-fg-muted">{e.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
