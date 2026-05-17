import unittest
from pathlib import Path

from static_extractors import ast_extract


FIXTURES = Path(__file__).parent / "fixtures"
REPO_PUBLISHER = FIXTURES / "repo_publisher"
REPO_CONSUMER = FIXTURES / "repo_consumer"


class AstExtractTests(unittest.TestCase):
    def test_python_basic_publish_is_extracted(self) -> None:
        edges = ast_extract.extract_messaging_edges(REPO_PUBLISHER, component_name="orders")
        producers = [e for e in edges if e.kind == "producesMessage"]
        self.assertGreaterEqual(len(producers), 1)
        # Should pick up channel.basic_publish(..., routing_key="shipping-task", ...)
        names = {e.queue_or_topic for e in producers}
        self.assertIn("shipping-task", names)

    def test_java_rabbit_listener_is_extracted_as_consumer(self) -> None:
        edges = ast_extract.extract_messaging_edges(REPO_CONSUMER, component_name="shipping")
        consumers = [e for e in edges if e.kind == "consumesMessage"]
        self.assertGreaterEqual(len(consumers), 1)
        names = {e.queue_or_topic for e in consumers}
        self.assertIn("shipping-task", names)

    def test_merge_adds_new_edges_when_llm_missed_them(self) -> None:
        edges = ast_extract.extract_messaging_edges(REPO_PUBLISHER, component_name="orders")
        # LLM extracted nothing for the queue.
        payload = {
            "repo": {"id": "fixture/repo_a"},
            "components": [],
            "apis": [],
            "resources": [],
            "providers": [],
            "domain_attributes": [],
            "glossary": [],
            "dependencies": [],
        }
        summary = ast_extract.merge_into_payload(payload, edges, component_name="orders")
        self.assertGreaterEqual(len(summary["added"]), 1)
        self.assertGreaterEqual(len(summary["resources_added"]), 1)
        # And the new edge appears in payload.
        kinds = {(d["source"], d["target"], d["kind"]) for d in payload["dependencies"]}
        self.assertIn(("orders", "shipping-task", "producesMessage"), kinds)

    def test_merge_corrects_kind_when_llm_had_wrong_direction(self) -> None:
        edges = ast_extract.extract_messaging_edges(REPO_PUBLISHER, component_name="orders")
        # LLM emitted the edge in the WRONG direction.
        payload = {
            "repo": {"id": "fixture/repo_a"},
            "components": [], "apis": [], "resources": [], "providers": [],
            "domain_attributes": [], "glossary": [],
            "dependencies": [
                {
                    "source": "orders",
                    "target": "shipping-task",
                    "target_kind": "resource",
                    "kind": "consumesMessage",   # WRONG — code says producesMessage
                    "protocol": "amqp",
                    "operation_or_usage": "",
                    "message_or_event_name": "",
                    "env_or_config_keys": [],
                    "aliases": [],
                    "confidence": "high",
                    "notes": "",
                    "evidence": [{"path": "src/orders.py", "line": 1, "snippet": "anything"}],
                }
            ],
        }
        summary = ast_extract.merge_into_payload(payload, edges, component_name="orders")
        self.assertEqual(len(summary["kind_corrected"]), 1)
        # Direction flipped to match the AST.
        self.assertEqual(payload["dependencies"][0]["kind"], "producesMessage")
        # Confidence demoted to review since the LLM and AST disagreed.
        self.assertEqual(payload["dependencies"][0]["confidence"], "review")

    def test_merge_marks_confirmation_when_llm_already_had_the_edge(self) -> None:
        edges = ast_extract.extract_messaging_edges(REPO_PUBLISHER, component_name="orders")
        payload = {
            "repo": {"id": "fixture/repo_a"},
            "components": [], "apis": [],
            "resources": [{"name": "shipping-task", "type": "queue", "technology": "RabbitMQ"}],
            "providers": [], "domain_attributes": [], "glossary": [],
            "dependencies": [
                {
                    "source": "orders",
                    "target": "shipping-task",
                    "target_kind": "resource",
                    "kind": "producesMessage",
                    "protocol": "amqp",
                    "operation_or_usage": "",
                    "message_or_event_name": "",
                    "env_or_config_keys": [],
                    "aliases": [],
                    "confidence": "medium",
                    "notes": "",
                    "evidence": [{"path": "src/orders.py", "line": 15, "snippet": "publish"}],
                }
            ],
        }
        summary = ast_extract.merge_into_payload(payload, edges, component_name="orders")
        self.assertEqual(len(summary["confirmed"]), 1)
        self.assertEqual(summary["added"], [])
        # Resource was already there → no resource_added entry for shipping-task.
        # The edge has AST evidence appended to it.
        self.assertTrue(any("_emitted_by" in d for d in payload["dependencies"]))

    def test_skip_test_directories(self) -> None:
        # The fixture has src/shipping_consumer.java (main) — would it find
        # something if we put a similar file under src/test? We don't, but
        # we can sanity-check that the helper would skip a test path.
        from pathlib import Path
        self.assertTrue(ast_extract._should_skip(Path("src/test/java/Foo.java")))
        self.assertTrue(ast_extract._should_skip(Path("src/tests/Bar.py")))
        self.assertFalse(ast_extract._should_skip(Path("src/main/Foo.java")))
        self.assertTrue(ast_extract._should_skip(Path("node_modules/foo/bar.js")))
        self.assertTrue(ast_extract._should_skip(Path("target/classes/Foo.java")))


if __name__ == "__main__":
    unittest.main()
