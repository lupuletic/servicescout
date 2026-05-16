import unittest

from build_catalog import derive_communication_flows
from crawler import communication_endpoint_keys


class CommunicationTopologyTests(unittest.TestCase):
    def test_derives_direct_queue_flow(self) -> None:
        payload = {
            "entities": [
                {"kind": "Component", "metadata": {"name": "producer"}, "spec": {}},
                {"kind": "Component", "metadata": {"name": "consumer"}, "spec": {}},
                {
                    "kind": "Resource",
                    "metadata": {"name": "orders.created", "annotations": {}},
                    "spec": {"type": "queue", "technology": "RabbitMQ"},
                },
            ],
            "relations": [
                {
                    "from": "Component:producer",
                    "type": "producesMessage",
                    "to": "Resource:orders.created",
                    "confidence": "high",
                    "evidence": [{"path": "producer.py", "line": 7, "snippet": "publish('orders.created')"}],
                    "properties": {"access": "publish"},
                },
                {
                    "from": "Component:consumer",
                    "type": "consumesMessage",
                    "to": "Resource:orders.created",
                    "confidence": "medium",
                    "evidence": [{"path": "consumer.py", "line": 11, "snippet": "subscribe('orders.created')"}],
                    "properties": {"access": "consume"},
                },
            ],
        }

        self.assertEqual(derive_communication_flows(payload), 1)
        flow = [r for r in payload["relations"] if r["type"] == "communicatesWith"][0]
        self.assertEqual(flow["from"], "Component:producer")
        self.assertEqual(flow["to"], "Component:consumer")
        self.assertEqual(flow["properties"]["endpoint"], "orders.created")
        self.assertEqual(flow["properties"]["transport"], "RabbitMQ")
        self.assertEqual(flow["properties"]["mechanism"], "async-message")
        self.assertEqual(flow["confidence"], "medium")

    def test_derives_api_flow_without_resource_nodes(self) -> None:
        payload = {
            "entities": [
                {"kind": "Component", "metadata": {"name": "client"}, "spec": {}},
                {"kind": "Component", "metadata": {"name": "server"}, "spec": {}},
            ],
            "relations": [
                {
                    "from": "Component:client",
                    "type": "consumesApi",
                    "to": "Component:server",
                    "confidence": "high",
                    "evidence": [{"path": "client.go", "line": 12, "snippet": "GET /v1/accounts"}],
                    "properties": {"protocol": "http", "operation_or_usage": "GET /v1/accounts"},
                },
            ],
        }

        self.assertEqual(derive_communication_flows(payload), 1)
        flow = [r for r in payload["relations"] if r["type"] == "communicatesWith"][0]
        self.assertEqual(flow["properties"]["transport"], "http")
        self.assertEqual(flow["properties"]["endpoint"], "GET /v1/accounts")

    def test_endpoint_discovery_role_suffixes_are_config_driven(self) -> None:
        without_roles = communication_endpoint_keys(
            "topic:orders.created.v1",
            allow_namespace_roles=True,
            role_suffixes=[],
        )
        with_roles = communication_endpoint_keys(
            "topic:orders.created.v1",
            allow_namespace_roles=True,
            role_suffixes=["producer"],
        )

        self.assertIn(("orders", "endpoint_namespace"), without_roles)
        self.assertNotIn(("ordersproducer", "endpoint_namespace_role"), without_roles)
        self.assertIn(("ordersproducer", "endpoint_namespace_role"), with_roles)


if __name__ == "__main__":
    unittest.main()
