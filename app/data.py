"""Synthetic webshop dataset, generated deterministically at startup.

Building this data and loading it into SQLite is the application's real
initialisation work, so a cold start has to pay for it (as a real service
that loads reference data or warms caches would).
"""
import sqlite3

import numpy as np
import pandas as pd

CATEGORIES = ["electronics", "books", "home", "garden", "toys", "sports", "fashion", "grocery"]
COUNTRIES = ["SK", "HU", "CZ", "AT", "DE", "PL"]


def build_dataset(n_orders: int = 200_000, n_products: int = 5_000, seed: int = 42):
    rng = np.random.default_rng(seed)

    products = pd.DataFrame({
        "product_id": np.arange(1, n_products + 1),
        "category": rng.choice(CATEGORIES, n_products),
        "price": np.round(rng.gamma(2.0, 25.0, n_products) + 1, 2),
    })
    orders = pd.DataFrame({
        "order_id": np.arange(1, n_orders + 1),
        "product_id": rng.integers(1, n_products + 1, n_orders),
        "quantity": rng.integers(1, 6, n_orders),
        "day": rng.integers(0, 365, n_orders),
        "country": rng.choice(COUNTRIES, n_orders),
    })
    orders = orders.merge(products[["product_id", "price", "category"]], on="product_id", how="left")
    orders["revenue"] = (orders["price"] * orders["quantity"]).round(2)
    return products, orders


def load_sqlite(products: pd.DataFrame, orders: pd.DataFrame) -> sqlite3.Connection:
    """In-memory SQLite database with indexes (shared across request threads)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    products.to_sql("products", conn, index=False)
    orders.to_sql("orders", conn, index=False)
    conn.execute("CREATE UNIQUE INDEX idx_orders_id ON orders(order_id)")
    conn.execute("CREATE INDEX idx_products_category ON products(category)")
    conn.commit()
    return conn
