package com.example.shipping;

import org.springframework.amqp.rabbit.annotation.RabbitListener;
import org.springframework.stereotype.Component;

@Component
public class ShippingConsumer {

    @RabbitListener(queues = "shipping-task")
    public void handle(byte[] message) {
        String orderId = new String(message);
        // dispatch shipping
    }
}
