import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { SWRConfig } from "swr";
import { AppShell } from "@/components/AppShell";
import { GraphPage } from "@/pages/GraphPage";
import { EntitiesPage } from "@/pages/EntitiesPage";
import { EntityPage } from "@/pages/EntityPage";
import { TriagePage } from "@/pages/TriagePage";
import { ActivityPage } from "@/pages/ActivityPage";
import { OperatorPage } from "@/pages/OperatorPage";
import { OnboardingPage } from "@/pages/OnboardingPage";
import "@react-sigma/core/lib/style.css";
import "./index.css";

const fetcher = (url: string) => fetch(url).then((r) => {
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <SWRConfig value={{ fetcher, revalidateOnFocus: false }}>
      <BrowserRouter>
        <AppShell>
          <Routes>
            <Route path="/" element={<GraphPage />} />
            <Route path="/entities" element={<EntitiesPage />} />
            <Route path="/entity/:ref" element={<EntityPage />} />
            <Route path="/triage" element={<TriagePage />} />
            <Route path="/activity" element={<ActivityPage />} />
            <Route path="/operator" element={<OperatorPage />} />
            <Route path="/onboard" element={<OnboardingPage />} />
          </Routes>
        </AppShell>
      </BrowserRouter>
    </SWRConfig>
  </StrictMode>,
);
