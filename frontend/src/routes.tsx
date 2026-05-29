import { lazy, Suspense } from "react";
import { Routes, Route } from "react-router-dom";
import { HomeGate } from "@/components/HomeGate";

const EntitiesPage = lazy(() => import("@/pages/EntitiesPage").then((module) => ({ default: module.EntitiesPage })));
const EntityPage = lazy(() => import("@/pages/EntityPage").then((module) => ({ default: module.EntityPage })));
const TriagePage = lazy(() => import("@/pages/TriagePage").then((module) => ({ default: module.TriagePage })));
const ActivityPage = lazy(() => import("@/pages/ActivityPage").then((module) => ({ default: module.ActivityPage })));
const OperatorPage = lazy(() => import("@/pages/OperatorPage").then((module) => ({ default: module.OperatorPage })));
const OnboardingPage = lazy(() => import("@/pages/OnboardingPage").then((module) => ({ default: module.OnboardingPage })));

function RouteFallback() {
  return (
    <div className="flex h-full items-center justify-center text-sm text-fg-muted">
      Loading view...
    </div>
  );
}

export function AppRoutes() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/" element={<HomeGate />} />
        <Route path="/entities" element={<EntitiesPage />} />
        <Route path="/entity/:ref" element={<EntityPage />} />
        <Route path="/triage" element={<TriagePage />} />
        <Route path="/activity" element={<ActivityPage />} />
        <Route path="/operator" element={<OperatorPage />} />
        <Route path="/onboard" element={<OnboardingPage />} />
      </Routes>
    </Suspense>
  );
}
