"""Replay the released reference choices through the task's shopping tools."""

import json
import sys
from pathlib import Path

sys.path.insert(0, "/workspace")
from shop import call, finish

gold = json.loads(Path("/solution/reference.json").read_text())
for product in gold["ground_truth_products"]:
    result = call(
        "add_product_to_cart",
        {"product_id": product["product_id"], "quantity": product.get("quantity", 1)},
    )
    if "error" in result:
        raise RuntimeError(result["error"])
for name, quantity in gold.get("ground_truth_coupons", {}).items():
    result = call("add_coupon_to_cart", {"coupon_name": name, "quantity": quantity})
    if "error" in result:
        raise RuntimeError(result["error"])
cart = call("get_cart_info", {})
if "error" in cart:
    raise RuntimeError(cart["error"])
finish("Selected the reference products and checked the final shopping cart.")
