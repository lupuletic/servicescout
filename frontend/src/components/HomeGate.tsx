import { Navigate } from "react-router-dom";
import useSWR from "swr";
import { GraphPage } from "@/pages/GraphPage";

// First-run empty state: with no catalog yet, land on the setup flow instead of
// an empty graph. Once repos are indexed, "/" is the Explorer.
export function HomeGate() {
  const { data, isLoading } = useSWR<{ summary?: { repos_indexed?: number } }>("/api/state.json");
  if (isLoading) return null;
  if ((data?.summary?.repos_indexed ?? 0) === 0) return <Navigate to="/onboard" replace />;
  return <GraphPage />;
}
