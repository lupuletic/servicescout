# ServiceScout Frontend

Operator UI for the ServiceScout catalog. The production build is served by
`dashboard.py` from `frontend/dist`; local development uses Vite and proxies
API calls to the FastAPI dashboard backend.

## Local Development

```bash
# from repo root
.venv/bin/python dashboard.py --catalog data/catalog.json --host 127.0.0.1 --port 8788
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173
```

Open `http://127.0.0.1:5173/`.

## Production Build

```bash
npm --prefix frontend run lint
npm --prefix frontend run build
```

The Docker image runs the same build and packages the static files into the
single ServiceScout image.

## Browser Tests

```bash
npm --prefix frontend exec playwright install chromium
npm --prefix frontend run test:e2e
```

The e2e suite starts a Vite server on `127.0.0.1:5174` and mocks the dashboard
API so the operator workflows can be checked without a live catalog.

## Product Surfaces

- `Explorer`: Sigma graph, kind filters, edge filters, confidence filters,
  and entity drilldown.
- `Catalog`: faceted entity table with confidence as a first-class filter.
- `Activity`: scheduler status, run history, tick drilldown, and trigger-now.
- `Operator`: cost trendline, verifier signal, staleness heatmap, and stale
  repo queue.
- `Triage`: verifier fact queue, owner assignment, terminal fact decisions,
  external-component decisions, and decision log.
