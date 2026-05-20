# Socks-Shop Onboarding Screenshots

Captured from an isolated eval workspace served at `http://127.0.0.1:8790`.

1. `03-explorer.png` — graph view with compact filters.
2. `04-catalog.png` — catalog facets with category descriptions.
3. `05-activity.png` — scheduler/activity view.
4. `06-operator.png` — operator metrics populated from the eval extraction run.
5. `07-triage.png` — triage surface.
6. `08-entity-front-end.png` — entity detail page.

To refresh these after a new local run:

```bash
./evals/setup.sh
./evals/build_eval_catalog.sh
python -m servicescout.build_kuzu --catalog evals/data/catalog.json --db evals/data/catalog.kuzu
docker compose --env-file .env.socks-shop.example up -d mcp dashboard
```
