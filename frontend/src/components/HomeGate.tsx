import { Navigate } from "react-router-dom";
import useSWR from "swr";
import { GraphPage } from "@/pages/GraphPage";

// First-run empty state: with no catalog yet, land on the setup flow instead of
// an empty graph. Once repos are indexed, "/" is the Explorer.
export function HomeGate() {
  const { data, error, isLoading } = useSWR<{ summary?: { repos_indexed?: number; entities?: number } }>("/api/state.json");
  if (isLoading) return null;
  // Redirect to setup ONLY on a confirmed-empty catalog. On an errored or
  // missing response, fail open to the Explorer — never trap the user on a
  // transient fetch failure.
  const summary = data?.summary;
  const confirmedEmpty = !error && !!summary && (summary.repos_indexed ?? 0) === 0 && (summary.entities ?? 0) === 0;
  if (confirmedEmpty) return <Navigate to="/onboard" replace />;
  return <GraphPage />;
}
