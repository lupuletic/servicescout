import { expect, test, type Page } from "@playwright/test";

const graphPayload = {
  nodes: [
    { id: "Component:orders", label: "orders", kind: "Component", system: "commerce", confidence: "high" },
    { id: "Component:payments", label: "payments", kind: "Component", system: "commerce", confidence: "medium" },
  ],
  edges: [
    {
      id: "e1",
      source: "Component:orders",
      target: "Component:payments",
      type: "communicatesWith",
      confidence: "high",
      properties: { transport: "http" },
    },
  ],
  truncated: false,
  node_total: 2,
  edge_total: 1,
};

const facetsPayload = {
  kind: [{ value: "Component", count: 2 }],
  type: [{ value: "service", count: 2 }],
  owner: [{ value: "platform", count: 1 }],
  lifecycle: [{ value: "production", count: 2 }],
  environment: [{ value: "prod", count: 2 }],
  tag: [{ value: "checkout", count: 1 }],
  runtime: [{ value: "node", count: 1 }],
  confidence: [
    { value: "high", count: 1 },
    { value: "review", count: 1 },
  ],
};

const entitiesPayload = {
  count: 2,
  entities: [
    {
      ref: "Component:orders",
      kind: "Component",
      name: "orders",
      tagline: "Order orchestration service",
      type: "service",
      source_repos: ["acme/orders"],
      confidence: "high",
    },
    {
      ref: "Component:legacy",
      kind: "Component",
      name: "legacy",
      tagline: "Needs verification",
      type: "service",
      source_repos: ["acme/legacy"],
      confidence: "review",
    },
  ],
};

const checkoutEntityPayload = {
  ref: "Component:checkoutservice",
  kind: "Component",
  name: "checkoutservice",
  confidence: "high",
  metadata: {
    name: "checkoutservice",
    description: "Checkout service",
    annotations: {
      tagline: "Places orders",
      source_repos: ["GoogleCloudPlatform/microservices-demo/checkoutservice"],
    },
  },
  spec: { type: "service", lifecycle: "production", owner: "platform" },
  evidence: [{ path: "main.go", line: 42, snippet: "func main()" }],
};

const crawlStatus = {
  lock: { held: false },
  scheduler: {
    running: false,
    pid: null,
    managed: true,
    source: "stopped",
    interval_minutes: 360,
    budget_usd: 20,
    log_path: null,
    log_tail: [],
  },
  last_run: null,
  interval_minutes: 360,
  budget_usd: 20,
};

const crawlRuns = {
  total: 1,
  runs: [
    {
      run_id: "20260519T100000Z-abc123",
      trigger: "cron",
      started_at: "2026-05-19T10:00:00+00:00",
      finished_at: "2026-05-19T10:04:00+00:00",
      status: "ok",
      repos_checked: 2,
      repos_changed_count: 1,
      budget_usd: 20,
      crawler_returncode: 0,
    },
  ],
};

const crawlRunDetail = {
  ...crawlRuns.runs[0],
  repos_changed: [{ repo: "orders", reason: "remote head moved" }],
  events: [
    { event: "change_detected", count: 1 },
    {
      event: "repo_clone_failed",
      repo: "acme/private-service",
      error: "remote: Organization has enabled or enforced SAML SSO. The requested URL returned error: 403",
    },
  ],
  crawler_stdout_tail: "crawler ok",
};

const workspaceConfig = {
  orgs: ["acme"],
  seeds: ["acme/storefront", "acme/mobile-app"],
  budget_usd: 100,
  scope: { max_discovery_rounds: 10 },
};

const auditPayload = {
  entries: [
    {
      ts: "2026-05-19T10:00:00+00:00",
      action: "crawl_triggered",
      repo: "acme/storefront",
      token_stored: false,
    },
  ],
};

const operatorSummary = {
  catalog: {
    last_build_at: "2026-05-19T10:00:00+00:00",
    repos_indexed: 2,
    entities: 12,
    relations: 24,
  },
  cost_trend: [
    { day: "2026-05-18", cost: 0.42, repos: 1, duration_seconds: 120 },
    { day: "2026-05-19", cost: 0.77, repos: 1, duration_seconds: 180 },
  ],
  staleness: {
    buckets: { fresh: 1, warm: 0, aging: 0, stale: 3, unknown: 0 },
    repos: [
      { repo: "legacy", extracted_at: "2026-05-01T10:00:00+00:00", age_hours: 430, cost: 0.2, status: "ok" },
      { repo: "checkout", extracted_at: "2026-05-08T10:00:00+00:00", age_hours: 262, cost: 1.1, status: "ok" },
      { repo: "broken", extracted_at: "2026-05-02T10:00:00+00:00", age_hours: 406, cost: 0.01, status: "error" },
    ],
    repo_total: 3,
  },
  verifier: {
    validation_errors: 0,
    evidence_quarantined: 1,
    repos_with_validation_errors: 0,
    repos_with_quarantined_evidence: 1,
    entity_confidence: { high: 8, medium: 3, review: 1 },
    relation_confidence: { high: 20, medium: 3, review: 1 },
  },
};

const factsPayload = {
  count: 1,
  assigned: 0,
  unassigned: 1,
  facts: [
    {
      id: "acme/orders:dependencies:0",
      repo: "acme/orders",
      category: "dependencies",
      label: "orders consumesApi payments",
      combined_verdict: "disconfirmed",
      explanation: "phase A: no evidence verified",
      phase_a_verdict: "disconfirmed",
      phase_b_verdict: "unsupported",
      confidence: "review",
      reasons: ["1 cited snippet(s) missing from file"],
      evidence: [{ path: "src/orders.ts", line: 12, snippet: "payments.charge(order)" }],
      current_fact: { source: "orders", target: "payments", kind: "consumesApi" },
    },
  ],
};

const triagePayload = { count: 0, components: [] };
const decisionsPayload = { total: 0, decisions: [] };

async function mockApis(page: Page) {
  await page.route("**/api/graph**", async (route) => route.fulfill({ json: graphPayload }));
  await page.route("**/api/facets", async (route) => route.fulfill({ json: facetsPayload }));
  await page.route("**/api/entities**", async (route) => route.fulfill({ json: entitiesPayload }));
  await page.route("**/api/entity/Component%3Acheckoutservice", async (route) => route.fulfill({ json: checkoutEntityPayload }));
  await page.route("**/api/crawl/status", async (route) => route.fulfill({ json: crawlStatus }));
  await page.route("**/api/crawl/runs/20260519T100000Z-abc123", async (route) => route.fulfill({ json: crawlRunDetail }));
  await page.route("**/api/crawl/runs", async (route) => route.fulfill({ json: crawlRuns }));
  await page.route("**/api/crawl/trigger", async (route) => route.fulfill({ status: 202, json: { status: "accepted", pid: 123 } }));
  await page.route("**/api/crawl/trigger/repos", async (route) => route.fulfill({ status: 202, json: { status: "accepted", pid: 125 } }));
  await page.route("**/api/crawl/trigger/repo**", async (route) => route.fulfill({ status: 202, json: { status: "accepted", pid: 124 } }));
  await page.route("**/api/workspace/config", async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ json: { status: "ok" } });
      return;
    }
    await route.fulfill({ json: workspaceConfig });
  });
  await page.route("**/api/audit**", async (route) => route.fulfill({ json: auditPayload }));
  await page.route("**/api/crawl/scheduler/start", async (route) => route.fulfill({
    status: 202,
    json: {
      status: "started",
      scheduler: {
        running: true,
        pid: 999,
        managed: true,
        source: "dashboard",
        interval_minutes: 360,
        budget_usd: 20,
      },
    },
  }));
  await page.route("**/api/crawl/scheduler/stop", async (route) => route.fulfill({
    json: {
      status: "stopped",
      scheduler: { running: false, pid: null, managed: true, source: "stopped" },
    },
  }));
  await page.route("**/api/operator/summary", async (route) => route.fulfill({ json: operatorSummary }));
  // HomeGate reads this to decide first-run empty-state vs Explorer; a populated
  // catalog keeps "/" on the graph.
  await page.route("**/api/state.json", async (route) => route.fulfill({ json: { summary: { repos_indexed: 2, entities: 12 } } }));
  await page.route("**/api/triage/facts", async (route) => route.fulfill({ json: factsPayload }));
  await page.route("**/api/triage/decisions", async (route) => route.fulfill({ json: decisionsPayload }));
  await page.route("**/api/triage.json", async (route) => route.fulfill({ json: triagePayload }));
  await page.route("**/api/triage/facts/decide", async (route) => route.fulfill({ json: { status: "ok" } }));
}

test.beforeEach(async ({ page }) => {
  await mockApis(page);
});

test("new crawl page summarizes saved seeds and starts a crawl", async ({ page }) => {
  await page.goto("/onboard");
  await expect(page.getByRole("heading", { name: "Add seed" })).toBeVisible();
  await expect(page.getByText("2 selected")).toBeVisible();
  await expect(page.getByText("acme/storefront", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Budget (USD)")).toHaveValue("100");
  await expect(page.getByLabel("Rounds")).toHaveValue("10");

  const configRequest = page.waitForRequest((request) =>
    request.url().endsWith("/api/workspace/config") && request.method() === "POST",
  );
  const triggerRequest = page.waitForRequest("**/api/crawl/trigger");
  await page.getByRole("button", { name: "Save and start crawl" }).click();
  await expect((await configRequest).postData() || "").toContain("acme/storefront");
  await expect((await triggerRequest).method()).toBe("POST");
  await expect(page.getByText("Crawl started")).toBeVisible();
});

test("explorer and catalog expose confidence as an operator filter", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Graph" })).toBeVisible();
  await expect(page.getByRole("button", { name: "high" })).toBeVisible();
  await expect(page.getByRole("button", { name: "review" })).toBeVisible();
  await expect(page.getByRole("button", { name: "API contracts" })).toBeDisabled();
  await expect(page.getByText("Flow map shows service-to-service communication.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Hide graph labels" })).toBeVisible();
  await page.getByRole("button", { name: "Hide graph labels" }).click();
  await expect(page.getByRole("button", { name: "Show graph labels" })).toBeVisible();

  await page.getByRole("button", { name: "Evidence graph" }).click();
  await expect(page.getByRole("button", { name: "API contracts" })).toBeEnabled();

  await page.getByRole("link", { name: "Catalog" }).click();
  await expect(page.getByRole("heading", { name: "Catalog" })).toBeVisible();
  await expect(page.getByRole("button", { name: "high 1" })).toBeVisible();
  await expect(page.getByRole("button", { name: "review 1" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "orders", exact: true })).toBeVisible();
});

test("activity can inspect a scheduler tick and trigger a crawl", async ({ page }) => {
  await page.goto("/activity");
  await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();
  await expect(page.getByText("Status", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Progress", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Spend", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Run progress")).toBeVisible();
  await expect(page.getByText("Automation", { exact: true })).toBeVisible();
  await expect(page.getByText("Crawler limits", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Interval (min)")).toHaveValue("360");
  await expect(page.getByLabel("Budget ($)")).toHaveValue("20");
  const runButton = page.getByRole("button").filter({ hasText: "20260519T100000Z-abc123" });
  await expect(runButton).toBeVisible();

  await runButton.click();
  await expect(page.getByText("remote head moved")).toBeVisible();
  await expect(page.getByText("Permission / SSO", { exact: true }).first()).toBeVisible();

  const triggerRequest = page.waitForRequest("**/api/crawl/trigger");
  await page.getByRole("button", { name: "Trigger now" }).click();
  await expect((await triggerRequest).method()).toBe("POST");

  const automationRequest = page.waitForRequest("**/api/crawl/scheduler/start");
  await page.getByRole("button", { name: "Enable" }).click();
  const automation = await automationRequest;
  expect(automation.method()).toBe("POST");
  expect(automation.postData() || "").toContain("interval_minutes");
  expect(automation.postData() || "").toContain("budget_usd");
});

test("activity surfaces an in-flight manual re-index before the final run log exists", async ({ page }) => {
  await page.route("**/api/crawl/status", async (route) => route.fulfill({
    json: {
      ...crawlStatus,
      lock: {
        held: true,
        pid: 69085,
        host: "test-host",
        acquired_at: "2026-05-25T08:40:30+00:00",
      },
      scheduler: {
        ...crawlStatus.scheduler,
        running: true,
        pid: 69085,
        source: "external",
        managed: false,
        budget_usd: 100,
      },
      crawler: { running: true, pid: 76257, uptime: "00:30" },
      active_run: {
        run_id: "20260525T084030Z-7a1cf39e",
        trigger: "manual-reindex",
        started_at: "2026-05-25T08:40:30+00:00",
        observed_at: "2026-05-25T08:55:00+00:00",
        status: "running",
        repos_checked: 77,
        repos_changed_count: 1,
        repos_completed_count: 1,
        budget_usd: 100,
        duration_seconds: 870,
        repos_changed: [{ repo: "payments", reason: "crawler_extraction", status: "ok" }],
        latest_batch: { n: 12, parallelism: 12, batch_size: 48, stale_remaining: 65 },
        active_workers: [
          {
            worker_id: "W1",
            repo: "orders",
            pid: 1001,
            phase: "command execution",
            last_message: "command execution running",
            elapsed_seconds: 120,
            last_at: "2026-05-25T08:54:30+00:00",
          },
          {
            worker_id: "W2",
            repo: "payments-api",
            pid: 1002,
            phase: "agent turn",
            last_message: "agent turn completed",
            elapsed_seconds: 95,
            last_at: "2026-05-25T08:54:45+00:00",
          },
        ],
        recent_completions: [
          {
            repo: "payments",
            status: "ok",
            duration_seconds: 54,
            cost_usd: 0.42,
            reason: "crawler_extraction",
            completed_at: "2026-05-25T08:53:00+00:00",
          },
        ],
        events: [
          { event: "tick_start", run_id: "20260525T084030Z-7a1cf39e", ts: "2026-05-25T08:40:30+00:00" },
          { event: "manual_reindex_selected", run_id: "20260525T084030Z-7a1cf39e", count: 77, requested: ["orders", "payments"] },
          { event: "crawler_invoke", run_id: "20260525T084030Z-7a1cf39e", repos: ["orders", "payments"] },
          { event: "extractor_process_start", repo: "orders", ts: "2026-05-25T08:52:30+00:00" },
          { event: "extractor_child_event", repo: "orders", line: "{\"event\":\"item_started\",\"item_type\":\"command_execution\",\"status\":\"in_progress\"}", ts: "2026-05-25T08:54:00+00:00" },
          { event: "repo_done", repo: "payments", status: "ok", duration_seconds: 54, cost_usd: 0.42, ts: "2026-05-25T08:53:00+00:00" },
        ],
      },
      active_log_path: "/tmp/crawl_trigger.log",
      active_log_tail: [
        "{\"event\":\"tick_start\",\"run_id\":\"20260525T084030Z-7a1cf39e\",\"ts\":\"2026-05-25T08:40:30+00:00\"}",
        "{\"event\":\"manual_reindex_selected\",\"run_id\":\"20260525T084030Z-7a1cf39e\",\"count\":77,\"requested\":[\"orders\",\"payments\"]}",
        "{\"event\":\"crawler_invoke\",\"run_id\":\"20260525T084030Z-7a1cf39e\",\"repos\":[\"orders\",\"payments\"]}",
      ],
    },
  }));

  await page.goto("/activity");
  await expect(page.getByText("20260525T084030Z-7a1cf39e").first()).toBeVisible();
  await expect(page.getByText("1/77")).toBeVisible();
  await expect(page.getByText("Workers", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("orders", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("command execution running").first()).toBeVisible();
  await page.getByRole("button", { name: "Events" }).click();
  await expect(page.getByText("Manual re-index selected: 77 repos").first()).toBeVisible();
});

test("entity source links support monorepo subdirectories", async ({ page }) => {
  await page.goto("/entity/Component%3Acheckoutservice");
  await expect(page.getByRole("heading", { name: "checkoutservice" })).toBeVisible();
  await expect(page.getByRole("link", { name: "GoogleCloudPlatform/microservices-demo" }).first()).toHaveAttribute(
    "href",
    "https://github.com/GoogleCloudPlatform/microservices-demo",
  );

  await page.getByRole("button", { name: "Evidence" }).click();
  await expect(page.getByRole("link", { name: "main.go:42" })).toHaveAttribute(
    "href",
    "https://github.com/GoogleCloudPlatform/microservices-demo/blob/main/main.go#L42",
  );
});

test("activity shows extraction runs before scheduler history exists", async ({ page }) => {
  const extractionRun = {
    run_id: "extraction-cartservice",
    trigger: "extraction",
    started_at: "2026-05-20T09:22:26+00:00",
    finished_at: "2026-05-20T09:29:07+00:00",
    status: "ok",
    repos_checked: 1,
    repos_changed_count: 1,
    cost_usd: 0.309364,
    duration_seconds: 401.109,
    crawler_returncode: 0,
  };
  await page.route("**/api/crawl/status", async (route) => route.fulfill({
    json: { ...crawlStatus, last_run: extractionRun },
  }));
  await page.route("**/api/crawl/runs/extraction-cartservice", async (route) => route.fulfill({
    json: {
      ...extractionRun,
      repos_changed: [{ repo: "cartservice", reason: "catalog extraction" }],
      events: [{ event: "repo_extracted", repo: "cartservice", cost_usd: 0.309364 }],
    },
  }));
  await page.route("**/api/crawl/runs", async (route) => route.fulfill({
    json: { total: 1, source: "extractions", runs: [extractionRun] },
  }));

  await page.goto("/activity");
  await expect(page.getByText("repos extracted")).toBeVisible();
  await expect(page.getByText("Spend", { exact: true }).first()).toBeVisible();
  const runTable = page.locator("section").filter({ hasText: "Started" });
  await expect(runTable.getByText("Cost", { exact: true })).toBeVisible();
  await expect(runTable.getByText("$0.31")).toBeVisible();
  const runButton = page.getByRole("button").filter({ hasText: "extraction-cartservice" });
  await expect(runButton).toBeVisible();

  await runButton.click();
  await expect(page.getByText("catalog extraction")).toBeVisible();
});

test("operator shows cost trend, verifier signal, and stale repositories", async ({ page }) => {
  await page.goto("/operator");
  await expect(page.getByRole("heading", { name: "Operator" })).toBeVisible();
  await expect(page.getByText("Cost Trendline")).toBeVisible();
  await expect(page.getByText("$1.19")).toHaveCount(2);
  await expect(page.getByText("Verifier Signal", { exact: true })).toBeVisible();
  await expect(page.getByText("Staleness Heatmap", { exact: true })).toBeVisible();
  await expect(page.getByText("legacy")).toBeVisible();
  await expect(page.getByText("Showing 3 of 3 repos due for re-index")).toBeVisible();

  await page.getByRole("button", { name: "Sort stale repositories by Cost" }).click();
  await expect(page.getByText("checkout")).toBeVisible();

  await page.getByLabel("Select legacy").check();
  await expect(page.getByText("1 selected")).toBeVisible();
  const reindexRequest = page.waitForRequest("**/api/crawl/trigger/repos");
  await page.getByRole("button", { name: "Re-index selected" }).click();
  const request = await reindexRequest;
  expect(request.method()).toBe("POST");
  expect(request.postData() || "").toContain("legacy");
});

test("triage lets an operator action a disconfirmed fact", async ({ page }) => {
  await page.goto("/triage");
  await expect(page.getByRole("heading", { name: "Triage" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Disconfirmed facts 1" })).toBeVisible();

  await page.getByText("orders consumesApi payments").click();
  await expect(page.getByText("1 cited snippet(s) missing from file")).toBeVisible();
  await page.getByPlaceholder("team or person").fill("platform");
  await page.getByPlaceholder("you@org.com").fill("ops@example.com");

  const decisionRequest = page.waitForRequest("**/api/triage/facts/decide");
  await page.getByRole("button", { name: "Record decision" }).click();
  const request = await decisionRequest;
  const body = request.postData() || "";
  expect(request.method()).toBe("POST");
  expect(body).toContain('name="owner"');
  expect(body).toContain("platform");
});
