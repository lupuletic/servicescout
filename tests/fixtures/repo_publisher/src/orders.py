"""Fixture: orders service that publishes to RabbitMQ."""

import os

import pika


def publish_order(order_id: str) -> None:
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=os.environ["RABBITMQ_HOST"])
    )
    channel = connection.channel()
    channel.queue_declare(queue="shipping-task", durable=True)
    channel.basic_publish(
        exchange="",
        routing_key="shipping-task",
        body=order_id.encode("utf-8"),
    )
    connection.close()
