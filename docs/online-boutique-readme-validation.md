# Online Boutique README Architecture Validation

Generated: 2026-05-20T14:25:43+00:00
Workspace: `online-boutique`
Catalog: `evals/workspaces/online-boutique/data/catalog.json`
Expectation: `evals/workspaces/online-boutique/readme_architecture.json`

## Summary

- PASS: 105
- WARN: 0
- FAIL: 0

## Findings

| Status | Check | Subject | Detail |
| --- | --- | --- | --- |
| PASS | `readme_component_mentioned` | `frontend` | README names this service |
| PASS | `readme_component_mentioned` | `cartservice` | README names this service |
| PASS | `readme_component_mentioned` | `productcatalogservice` | README names this service |
| PASS | `readme_component_mentioned` | `currencyservice` | README names this service |
| PASS | `readme_component_mentioned` | `paymentservice` | README names this service |
| PASS | `readme_component_mentioned` | `shippingservice` | README names this service |
| PASS | `readme_component_mentioned` | `emailservice` | README names this service |
| PASS | `readme_component_mentioned` | `checkoutservice` | README names this service |
| PASS | `readme_component_mentioned` | `recommendationservice` | README names this service |
| PASS | `readme_component_mentioned` | `adservice` | README names this service |
| PASS | `readme_component_mentioned` | `loadgenerator` | README names this service |
| PASS | `readme_flow_endpoints_mentioned` | `loadgenerator -> frontend` | loadgenerator sends HTTP traffic to frontend |
| PASS | `readme_flow_endpoints_mentioned` | `frontend -> adservice` | frontend calls ad |
| PASS | `readme_flow_endpoints_mentioned` | `frontend -> recommendationservice` | frontend calls recommendation |
| PASS | `readme_flow_endpoints_mentioned` | `frontend -> productcatalogservice` | frontend calls productcatalog |
| PASS | `readme_flow_endpoints_mentioned` | `frontend -> cartservice` | frontend calls cart |
| PASS | `readme_flow_endpoints_mentioned` | `frontend -> checkoutservice` | frontend calls checkout |
| PASS | `readme_flow_endpoints_mentioned` | `checkoutservice -> cartservice` | checkout retrieves cart |
| PASS | `readme_flow_endpoints_mentioned` | `checkoutservice -> productcatalogservice` | checkout reads product data |
| PASS | `readme_flow_endpoints_mentioned` | `checkoutservice -> currencyservice` | checkout prices in requested currency |
| PASS | `readme_flow_endpoints_mentioned` | `checkoutservice -> paymentservice` | checkout charges cards |
| PASS | `readme_flow_endpoints_mentioned` | `checkoutservice -> shippingservice` | checkout books shipping |
| PASS | `readme_flow_endpoints_mentioned` | `checkoutservice -> emailservice` | checkout sends order confirmation email |
| PASS | `component_present` | `frontend` | README service exists as Component |
| PASS | `component_type` | `frontend` | type=website expected one of ['website', 'service'] |
| PASS | `component_language` | `frontend` | expected language terms ['go'] |
| PASS | `component_role` | `frontend` | expected role terms ['http', 'server', 'website', 'storefront'] |
| PASS | `component_present` | `cartservice` | README service exists as Component |
| PASS | `component_type` | `cartservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `cartservice` | expected language terms ['c#', 'asp.net', '.net'] |
| PASS | `component_role` | `cartservice` | expected role terms ['cart', 'redis'] |
| PASS | `component_present` | `productcatalogservice` | README service exists as Component |
| PASS | `component_type` | `productcatalogservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `productcatalogservice` | expected language terms ['go'] |
| PASS | `component_role` | `productcatalogservice` | expected role terms ['product', 'catalog', 'search'] |
| PASS | `component_present` | `currencyservice` | README service exists as Component |
| PASS | `component_type` | `currencyservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `currencyservice` | expected language terms ['node.js', 'node', 'javascript'] |
| PASS | `component_role` | `currencyservice` | expected role terms ['currency', 'conversion'] |
| PASS | `component_present` | `paymentservice` | README service exists as Component |
| PASS | `component_type` | `paymentservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `paymentservice` | expected language terms ['node.js', 'node', 'javascript'] |
| PASS | `component_role` | `paymentservice` | expected role terms ['payment', 'credit', 'card'] |
| PASS | `component_present` | `shippingservice` | README service exists as Component |
| PASS | `component_type` | `shippingservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `shippingservice` | expected language terms ['go'] |
| PASS | `component_role` | `shippingservice` | expected role terms ['shipping', 'cost', 'quote'] |
| PASS | `component_present` | `emailservice` | README service exists as Component |
| PASS | `component_type` | `emailservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `emailservice` | expected language terms ['python'] |
| PASS | `component_role` | `emailservice` | expected role terms ['email', 'confirmation'] |
| PASS | `component_present` | `checkoutservice` | README service exists as Component |
| PASS | `component_type` | `checkoutservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `checkoutservice` | expected language terms ['go'] |
| PASS | `component_role` | `checkoutservice` | expected role terms ['checkout', 'order', 'payment', 'shipping'] |
| PASS | `component_present` | `recommendationservice` | README service exists as Component |
| PASS | `component_type` | `recommendationservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `recommendationservice` | expected language terms ['python'] |
| PASS | `component_role` | `recommendationservice` | expected role terms ['recommendation', 'product'] |
| PASS | `component_present` | `adservice` | README service exists as Component |
| PASS | `component_type` | `adservice` | type=service expected one of ['service'] |
| PASS | `component_language` | `adservice` | expected language terms ['java'] |
| PASS | `component_role` | `adservice` | expected role terms ['ad', 'ads'] |
| PASS | `component_present` | `loadgenerator` | README service exists as Component |
| PASS | `component_type` | `loadgenerator` | type=worker expected one of ['worker', 'service'] |
| PASS | `component_language` | `loadgenerator` | expected language terms ['python', 'locust'] |
| PASS | `component_role` | `loadgenerator` | expected role terms ['load', 'traffic', 'frontend'] |
| PASS | `readme_flow_present` | `loadgenerator -> frontend` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `frontend -> adservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `frontend -> recommendationservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `frontend -> productcatalogservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `frontend -> cartservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `frontend -> checkoutservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `checkoutservice -> cartservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `checkoutservice -> productcatalogservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `checkoutservice -> currencyservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `checkoutservice -> paymentservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `checkoutservice -> shippingservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `readme_flow_present` | `checkoutservice -> emailservice` | relations=['communicatesWith', 'consumesApi'] |
| PASS | `resource_terms` | `redis-cart` | expected terms ['redis', 'cart'] |
| PASS | `resource_used_by_expected_component` | `redis-cart` | cartservice relations=['readsResource', 'writesResource'] |
| PASS | `extra_component_allowed` | `Packaging-Service` | Optional external packaging dependency guarded by PACKAGING_SERVICE_URL in frontend source. |
| PASS | `extra_component_allowed` | `shoppingassistantservice` | Optional Gemini/AlloyDB assistant component present in current microservices-demo source but not in the base README 11-service diagram. |
| PASS | `source_repo_root` | `Component:frontend` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:frontend` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:cartservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:cartservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:productcatalogservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:productcatalogservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:currencyservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:currencyservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:paymentservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:paymentservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:shippingservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:shippingservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:emailservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:emailservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:checkoutservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:checkoutservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:recommendationservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:recommendationservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:adservice` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:adservice` | https://github.com/GoogleCloudPlatform/microservices-demo |
| PASS | `source_repo_root` | `Component:loadgenerator` | roots=['GoogleCloudPlatform/microservices-demo'] expected=GoogleCloudPlatform/microservices-demo |
| PASS | `source_location_precise` | `Component:loadgenerator` | https://github.com/GoogleCloudPlatform/microservices-demo |
