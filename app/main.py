"""Webshop analytics API - the application measured in the Cloud Run cold start experiment.

A mid-sized FastAPI service: real dependencies (FastAPI, pandas, NumPy), a
200,000-row dataset built and loaded into SQLite at startup, and endpoints with
different costs. Every JSON response carries instance metadata, including
cold=true for the first request served by an instance, so cold starts are
detected from the server side and not only inferred from latency.

Endpoints:
  /                     light: instance info only
  /api/products         SQLite query (?category=books&limit=20)
  /api/orders/{id}      SQLite primary-key lookup
  /api/report           heavy: pandas aggregation over all orders
  /health               plain "ok" (not counted in the request counter)
"""
import os
import threading
import time
import uuid

PROCESS_START = time.time()

from fastapi import FastAPI, HTTPException  # noqa: E402  (imports are part of the startup cost)
from data import CATEGORIES, build_dataset, load_sqlite  # noqa: E402

INSTANCE_ID = uuid.uuid4().hex[:8]
VARIANT = os.environ.get("VARIANT", "local")
REVISION = os.environ.get("K_REVISION", "local")

# --- Initialisation (this is what a cold start pays for) ---
_t = time.perf_counter()
PRODUCTS, ORDERS = build_dataset()
DB = load_sqlite(PRODUCTS, ORDERS)
DB_LOCK = threading.Lock()
STARTUP_MS = round((time.time() - PROCESS_START) * 1000, 1)
INIT_MS = round((time.perf_counter() - _t) * 1000, 1)

_count_lock = threading.Lock()
_request_count = 0

app = FastAPI(title="Webshop analytics", docs_url=None, redoc_url=None)


def respond(data, t0: float) -> dict:
    """Wraps a payload with instance metadata; counts the request."""
    global _request_count
    with _count_lock:
        _request_count += 1
        count = _request_count
    return {
        "variant": VARIANT,
        "revision": REVISION,
        "instance_id": INSTANCE_ID,
        "cold": count == 1,
        "request_count": count,
        "instance_uptime_s": round(time.time() - PROCESS_START, 2),
        "startup_ms": STARTUP_MS,
        "init_ms": INIT_MS,
        "work_ms": round((time.perf_counter() - t0) * 1000, 2),
        "data": data,
    }


@app.get("/health")
def health():
    return "ok"


@app.get("/")
def index():
    t0 = time.perf_counter()
    return respond({"orders": len(ORDERS), "products": len(PRODUCTS)}, t0)


@app.get("/api/products")
def products(category: str | None = None, limit: int = 20):
    t0 = time.perf_counter()
    limit = max(1, min(limit, 100))
    if category is not None and category not in CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category must be one of {CATEGORIES}")
    sql = "SELECT product_id, category, price FROM products"
    args: list = []
    if category:
        sql += " WHERE category = ?"
        args.append(category)
    sql += " ORDER BY price DESC LIMIT ?"
    args.append(limit)
    with DB_LOCK:
        rows = DB.execute(sql, args).fetchall()
    return respond([{"product_id": r[0], "category": r[1], "price": r[2]} for r in rows], t0)


@app.get("/api/orders/{order_id}")
def order(order_id: int):
    t0 = time.perf_counter()
    with DB_LOCK:
        row = DB.execute(
            "SELECT order_id, product_id, quantity, day, country, price, category, revenue "
            "FROM orders WHERE order_id = ?",
            (order_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")
    keys = ["order_id", "product_id", "quantity", "day", "country", "price", "category", "revenue"]
    return respond(dict(zip(keys, row)), t0)


@app.get("/api/report")
def report():
    t0 = time.perf_counter()
    by_category = (
        ORDERS.groupby("category")
        .agg(orders=("order_id", "count"), revenue=("revenue", "sum"), avg_order=("revenue", "mean"))
        .round(2)
        .reset_index()
        .sort_values("revenue", ascending=False)
    )
    by_country = ORDERS.groupby("country")["revenue"].sum().round(2).sort_values(ascending=False)
    monthly = (ORDERS.assign(month=ORDERS["day"] // 30).groupby("month")["revenue"].sum().round(2))
    top_products = (
        ORDERS.groupby("product_id")["revenue"].sum().nlargest(5).round(2).reset_index()
    )
    data = {
        "by_category": by_category.to_dict(orient="records"),
        "by_country": by_country.to_dict(),
        "monthly_revenue": {int(k): float(v) for k, v in monthly.items()},
        "top_products": top_products.to_dict(orient="records"),
    }
    return respond(data, t0)
