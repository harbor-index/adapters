{{ system_prompt }}

## User request

{{ query }}

## Tool interface

The product catalog, user profile and shopping cart are in `/workspace/state`.
Use the provided shopping tools through the terminal:

```sh
python /workspace/shop.py list
python /workspace/shop.py get_user_info '{}'
python /workspace/shop.py search_products '{"query": "running shoes", "limit": 10}'
python /workspace/shop.py get_product_details '{"product_ids": ["PRODUCT_ID"]}'
python /workspace/shop.py add_product_to_cart '{"product_id": "PRODUCT_ID", "quantity": 1}'
python /workspace/shop.py get_cart_info '{}'
```

`list` prints the exact tool names, schemas and descriptions. Tool output is JSON.
The tools save cart changes to `/workspace/state/cart.json` and record interactions
in `/workspace/state/messages.json`. When the cart is ready, record your final
summary and complete the interaction:

```sh
python /workspace/shop.py finish 'Your final shopping summary and explanation'
```
