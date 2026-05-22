"""Guards for the dependency-graph scorer's matching logic.

The scorer is the experiment's yardstick; if its edge resolution is too lenient
it manufactures false confidence (the exact failure we're guarding against). So
we pin: indirect API-node resolution, name-drift token matching, hallucinated-
edge detection, and resource token matching.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import score_dependency_graph as sdg  # noqa: E402


def gold() -> sdg.Gold:
    return sdg.Gold(
        components={"frontend", "cartservice", "productcatalogservice", "adservice"},
        service_edges={("frontend", "cartservice"), ("frontend", "productcatalogservice")},
        resource_edges={("cartservice", "redis-cart")},
        documented={("frontend", "cartservice"), ("cartservice", "redis-cart")},
        resource_tokens={"redis-cart": ["redis", "cart"]},
    )


class ResolverTests(unittest.TestCase):
    def test_token_match_handles_name_drift(self) -> None:
        resolve = sdg.make_resolver({"frontend", "cartservice"})
        self.assertEqual(resolve("Component:googlecloudplatform-microservices-demo-frontend"), "frontend")
        self.assertEqual(resolve("Component:cartservice"), "cartservice")
        self.assertIsNone(resolve("Component:totally-unrelated"))

    def test_spaced_name_resolves(self) -> None:
        # Raw extractions name targets pre-canonicalisation, e.g. with spaces.
        resolve = sdg.make_resolver({"productcatalogservice", "cartservice"})
        self.assertEqual(resolve("Product Catalog Service"), "productcatalogservice")
        self.assertEqual(resolve("Cart Service"), "cartservice")


class PredictedEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gold = gold()
        self.resolve = sdg.make_resolver(self.gold.components)

    def test_indirect_api_node_resolves_to_provider(self) -> None:
        catalog = {"relations": [
            {"from": "Component:productcatalogservice", "type": "providesApi", "to": "API:ProductCatalogService"},
            {"from": "Component:frontend", "type": "consumesApi", "to": "API:ProductCatalogService"},
        ]}
        service, _ = sdg.predicted_edges(catalog, self.gold, self.resolve)
        self.assertIn(("frontend", "productcatalogservice"), service)

    def test_direct_communicates_with(self) -> None:
        catalog = {"relations": [
            {"from": "Component:frontend", "type": "communicatesWith", "to": "Component:cartservice"},
        ]}
        service, _ = sdg.predicted_edges(catalog, self.gold, self.resolve)
        self.assertIn(("frontend", "cartservice"), service)

    def test_resource_edge_token_match(self) -> None:
        catalog = {"relations": [
            {"from": "Component:cartservice", "type": "readsResource", "to": "Resource:redis-cart"},
        ]}
        _, resource = sdg.predicted_edges(catalog, self.gold, self.resolve)
        self.assertEqual(
            sdg.match_resource(resource, self.gold.resource_edges, self.gold.resource_tokens),
            {("cartservice", "redis-cart")},
        )


class ScoringTests(unittest.TestCase):
    def _score(self, relations, beta=1.0):
        import json, tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"entities": [], "relations": relations}, fh)
            path = Path(fh.name)
        return sdg.score_one(path, gold(), beta)

    def test_hallucinated_edge_lowers_precision(self) -> None:
        # ad->cart does not exist in gold; must be a false positive.
        s = self._score([
            {"from": "Component:frontend", "type": "communicatesWith", "to": "Component:cartservice"},
            {"from": "Component:adservice", "type": "communicatesWith", "to": "Component:cartservice"},
        ])
        self.assertIn(("adservice", "cartservice"), s.fp)
        self.assertLess(s.precision, 1.0)

    def test_perfect_catalog_scores_one(self) -> None:
        s = self._score([
            {"from": "Component:frontend", "type": "communicatesWith", "to": "Component:cartservice"},
            {"from": "Component:frontend", "type": "communicatesWith", "to": "Component:productcatalogservice"},
            {"from": "Component:cartservice", "type": "readsResource", "to": "Resource:redis-cart"},
        ])
        self.assertEqual(s.precision, 1.0)
        self.assertEqual(s.recall, 1.0)
        self.assertEqual(s.fbeta, 1.0)

    def test_score_extractions_restricts_gold_to_extracted_sources(self) -> None:
        # One extraction for recommendationservice that found its single edge.
        # Gold is restricted to that source, so recall isn't dragged down by
        # the 14 other gold edges from services we didn't extract.
        import json, tempfile
        ex = {
            "repo": {"id": "GoogleCloudPlatform/microservices-demo/recommendationservice"},
            "dependencies": [
                {"source": "recommendationservice", "target": "Product Catalog Service",
                 "target_kind": "component", "kind": "consumesApi"},
            ],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(ex, fh)
            path = Path(fh.name)
        gold = sdg.Gold(
            components={"recommendationservice", "productcatalogservice", "frontend"},
            service_edges={("recommendationservice", "productcatalogservice"),
                           ("frontend", "productcatalogservice")},
            resource_edges=set(),
            documented={("frontend", "productcatalogservice")},
            resource_tokens={},
        )
        s = sdg.score_extractions([path], gold, beta=1.0)
        self.assertEqual(s.recall, 1.0)     # only the recommendation edge is in scope
        self.assertEqual(s.precision, 1.0)  # spaced name resolved, no hallucination
        self.assertEqual(s.fbeta, 1.0)

    def test_undocumented_recall_isolates_README_invisible_edges(self) -> None:
        # Only the documented frontend->cartservice present; the undocumented
        # frontend->productcatalog is missing -> undocumented recall must drop.
        s = self._score([
            {"from": "Component:frontend", "type": "communicatesWith", "to": "Component:cartservice"},
            {"from": "Component:cartservice", "type": "readsResource", "to": "Resource:redis-cart"},
        ])
        self.assertEqual(s.documented_recall, 1.0)
        self.assertEqual(s.undocumented_recall, 0.0)


if __name__ == "__main__":
    unittest.main()
