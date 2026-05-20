"""Tests for the language-agnostic mini-extractors.

These exercise:
  - Dockerfile parsing (FROM, EXPOSE → runtime / Provider hints)
  - k8s manifest parsing (Deployment, CronJob with schedule, Ingress → API)
  - Build-manifest parsing (package.json → providers + resources + framework tags)
  - API spec parsing (OpenAPI, .proto → API entities + operations)
  - Unified runner + merge
"""

import unittest
from pathlib import Path

from servicescout.static_extractors.mini import (
    api_specs,
    build_manifests,
    dockerfile,
    kubernetes,
    runner,
)


FIXTURES = Path(__file__).parent / "fixtures" / "repo_mini"


class DockerfileTests(unittest.TestCase):
    def test_extracts_java_runtime_and_provider_hint(self) -> None:
        facts = dockerfile.extract(FIXTURES / "Dockerfile", FIXTURES)
        runtimes = [f for f in facts if f.body.get("name") == "repo_mini" and "java" in (f.body.get("tags") or [])]
        self.assertTrue(runtimes)
        # The fixture uses `openjdk:11-jre-slim` from Docker Hub —
        # no explicit registry → no Provider entity (DockerHub is implicit).
        providers = [f for f in facts if f.category == "providers"]
        self.assertEqual(providers, [])

    def test_extracts_port_from_expose(self) -> None:
        facts = dockerfile.extract(FIXTURES / "Dockerfile", FIXTURES)
        port_tags = [t for f in facts for t in f.body.get("tags", []) if t.startswith("port:")]
        self.assertIn("port:8080", port_tags)


class KubernetesTests(unittest.TestCase):
    def test_extracts_deployment_cronjob_ingress(self) -> None:
        facts = kubernetes.extract(FIXTURES / "deployment.yaml", FIXTURES)
        names = sorted({f.body.get("name") for f in facts})
        self.assertIn("my-service", names)
        self.assertIn("nightly-batch", names)
        self.assertIn("my-ingress", names)
        # CronJob carries schedule tag.
        cron = next(f for f in facts if f.body.get("name") == "nightly-batch")
        self.assertEqual(cron.body.get("type"), "cron")
        self.assertIn("schedule:0 2 * * *", cron.body.get("tags", []))
        # Ingress is an API with operation paths.
        ingress = next(f for f in facts if f.body.get("name") == "my-ingress")
        self.assertEqual(ingress.category, "apis")
        op_paths = [op["path"] for op in ingress.body.get("operations", [])]
        self.assertIn("/api/v1/foo", op_paths)


class BuildManifestTests(unittest.TestCase):
    def test_package_json_emits_providers_and_resources(self) -> None:
        facts = build_manifests.extract(FIXTURES / "package.json", FIXTURES)
        provider_names = {f.body.get("name") for f in facts if f.category == "providers"}
        resource_techs = {f.body.get("technology") for f in facts if f.category == "resources"}
        self.assertIn("Stripe", provider_names)
        self.assertIn("Sentry", provider_names)
        self.assertIn("Kafka", resource_techs)
        self.assertIn("PostgreSQL", resource_techs)

    def test_package_json_does_not_emit_arbitrary_deps(self) -> None:
        # `left-pad` is in the fixture but not in our provider/resource
        # tables — it should NOT produce a fact (we curate).
        facts = build_manifests.extract(FIXTURES / "package.json", FIXTURES)
        all_names = {f.body.get("name") for f in facts}
        self.assertNotIn("left-pad", all_names)

    def test_framework_tag_for_express(self) -> None:
        facts = build_manifests.extract(FIXTURES / "package.json", FIXTURES)
        component_tags: set = set()
        for f in facts:
            if f.category == "components":
                component_tags |= set(f.body.get("tags") or [])
        self.assertIn("express", component_tags)


class ApiSpecTests(unittest.TestCase):
    def test_openapi_yaml_emits_api_with_operations(self) -> None:
        facts = api_specs.extract(FIXTURES / "openapi.yaml", FIXTURES)
        self.assertEqual(len(facts), 1)
        api = facts[0]
        self.assertEqual(api.body.get("type"), "openapi")
        op_ids = {op["name"] for op in api.body.get("operations", [])}
        self.assertIn("getCustomer", op_ids)
        self.assertIn("updateCustomer", op_ids)
        self.assertIn("createCustomer", op_ids)

    def test_proto_emits_grpc_service(self) -> None:
        facts = api_specs.extract(FIXTURES / "service.proto", FIXTURES)
        self.assertEqual(len(facts), 1)
        api = facts[0]
        self.assertEqual(api.body.get("type"), "grpc")
        self.assertEqual(api.body.get("name"), "GreeterService")
        op_names = {op["name"] for op in api.body.get("operations", [])}
        self.assertEqual(op_names, {"SayHello", "SayGoodbye"})


class RunnerTests(unittest.TestCase):
    def test_extracts_everything_from_fixture(self) -> None:
        facts = runner.extract_all(FIXTURES)
        by_cat: dict[str, int] = {}
        for f in facts:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
        self.assertGreaterEqual(by_cat.get("components", 0), 1)
        self.assertGreaterEqual(by_cat.get("apis", 0), 2)        # ingress + openapi + proto = 3
        self.assertGreaterEqual(by_cat.get("providers", 0), 1)   # Stripe / Sentry
        self.assertGreaterEqual(by_cat.get("resources", 0), 1)   # Kafka / PostgreSQL

    def test_merge_into_empty_payload(self) -> None:
        facts = runner.extract_all(FIXTURES)
        payload: dict = {
            "repo": {"id": "fixture/repo_mini"},
            "components": [],
            "apis": [],
            "resources": [],
            "providers": [],
            "domain_attributes": [],
            "glossary": [],
            "dependencies": [],
        }
        summary = runner.merge_into_payload(payload, facts)
        self.assertGreater(summary["added"], 0)
        # API entities present.
        api_names = {a["name"] for a in payload["apis"]}
        self.assertIn("GreeterService", api_names)
        self.assertIn("Customer API", api_names)

    def test_merge_deduplicates_by_name(self) -> None:
        # Pre-seed an API with the same name as the OpenAPI fixture.
        payload: dict = {
            "repo": {"id": "fixture/repo_mini"},
            "components": [],
            "apis": [{"name": "Customer API", "type": "rest", "exposed_by": "x",
                       "operations": [], "notes": "from LLM", "evidence": []}],
            "resources": [],
            "providers": [],
            "domain_attributes": [],
            "glossary": [],
            "dependencies": [],
        }
        facts = runner.extract_all(FIXTURES)
        summary = runner.merge_into_payload(payload, facts)
        # API count stays at one for Customer API — merged, not duplicated.
        cust_apis = [a for a in payload["apis"] if a["name"] == "Customer API"]
        self.assertEqual(len(cust_apis), 1)
        # The mini-extractor's evidence was appended.
        self.assertGreater(summary["merged"], 0)


if __name__ == "__main__":
    unittest.main()
