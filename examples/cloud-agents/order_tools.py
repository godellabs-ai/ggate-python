"""Deterministic business tools; no external orders are changed."""


def read_order(order_id: str) -> dict:
    """Read the product and quantity for a customer order."""
    return {
        "order_id": order_id,
        "product": "Standing desk",
        "quantity": 2,
        "destination": "Sydney",
    }


def check_inventory(product: str) -> dict:
    """Check whether the requested product is available."""
    return {"product": product, "available": 18, "warehouse": "Sydney"}


def calculate_shipping(destination: str) -> dict:
    """Calculate shipping for the order's destination."""
    return {"destination": destination, "cost_aud": 35, "business_days": 2}


def check_return_policy(product: str) -> dict:
    """Read the return window for the product."""
    return {
        "product": product,
        "return_window_days": 30,
        "condition": "Original packaging",
    }


def prepare_dispatch(order_id: str) -> dict:
    """Prepare a dispatch recommendation for human review; no order is changed."""
    return {"order_id": order_id, "status": "Ready for review", "warehouse": "Sydney"}


TOOLS = [
    read_order,
    check_inventory,
    calculate_shipping,
    check_return_policy,
    prepare_dispatch,
]
