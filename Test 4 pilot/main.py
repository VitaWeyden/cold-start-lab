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

Test 4 additions (image shop-api:v4; data, endpoints and startup work unchanged):
  * structured JSON log lines on stdout (Cloud Logging stores them as jsonPayload):
      instance_start     after the dataset is loaded (instance_id, startup_ms, init_ms)
      instance_identity  the Cloud Run instance id from the metadata server, so the app's
                         instance_id can be joined with labels.instanceId of every log entry
      instance_ready     the server is about to accept requests (lifespan startup)
      instance_shutdown  lifespan shutdown, i.e. after SIGTERM (request_count, uptime)
      request            one line per request (request_id, instance_id, path, status, cold)
  * the X-Request-Id header sent by measure4.py is echoed as request_id in the response
  * cloud_run_instance_id in every response (empty until the metadata lookup has finished)
The metadata lookup runs in a background thread AFTER startup_ms is taken, so it does not
change the measured startup work.
"""
import contextvars
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

PROCESS_START = time.time()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402  (imports are part of the startup cost)
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
_request_id = contextvars.ContextVar("request_id", default="")
CLOUD_RUN_INSTANCE_ID = ""


def utc_iso(t: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).isoformat(timespec="milliseconds")


def log_event(event: str, **fields) -> None:
    """One JSON line on stdout. Cloud Run sends it to Cloud Logging as jsonPayload."""
    rec = {"severity": "INFO", "message": event, "event": event, "instance_id": INSTANCE_ID,
           "cloud_run_instance_id": CLOUD_RUN_INSTANCE_ID, "revision": REVISION,
           "t_utc": utc_iso(), **fields}
    sys.stdout.write(json.dumps(rec) + "\n")
    sys.stdout.flush()


log_event("instance_start", process_start_utc=utc_iso(PROCESS_START),
          startup_ms=STARTUP_MS, init_ms=INIT_MS)


def _lookup_cloud_run_instance_id() -> None:
    """Cloud Run instance id from the metadata server (only exists on Cloud Run)."""
    global CLOUD_RUN_INSTANCE_ID
    req = urllib.request.Request(
        "http://metadata.google.internal/computeMetadata/v1/instance/id",
        headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            CLOUD_RUN_INSTANCE_ID = r.read().decode().strip()
        log_event("instance_identity")
    except Exception as e:  # noqa: BLE001
        log_event("instance_identity", error=repr(e))


if os.environ.get("K_SERVICE"):  # set by Cloud Run; skipped locally
    threading.Thread(target=_lookup_cloud_run_instance_id, daemon=True).start()


@asynccontextmanager
async def lifespan(_app):
    log_event("instance_ready", since_process_start_ms=round((time.time() - PROCESS_START) * 1000, 1))
    yield
    # uvicorn runs this after SIGTERM, once open connections are closed
    log_event("instance_shutdown", request_count=_request_count,
              instance_uptime_s=round(time.time() - PROCESS_START, 2))


app = FastAPI(title="Webshop analytics", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def request_log(request: Request, call_next):
    rid = request.headers.get("x-request-id", "")
    tok = _request_id.set(rid)
    t0 = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        _request_id.reset(tok)
        if request.url.path != "/health":
            log_event("request", request_id=rid, path=request.url.path, status=status,
                      request_count=_request_count, server_ms=round((time.perf_counter() - t0) * 1000, 2))


def respond(data, t0: float) -> dict:
    """Wraps a payload with instance metadata; counts the request."""
    global _request_count
    with _count_lock:
        _request_count += 1
        count = _request_count
    return {
        "request_id": _request_id.get(),
        "cloud_run_instance_id": CLOUD_RUN_INSTANCE_ID,
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
