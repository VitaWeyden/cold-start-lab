#!/usr/bin/env python3
"""Builds the model file of sizes M and L at image build time (no download, deterministic).

A RandomForestRegressor that predicts an order's revenue from (price, quantity, day). The data
is synthetic with noise, so the trees grow deep and the file size is controlled by the number
of trees: trees are added one by one until the pickled size reaches --target-mb.

  python make_model.py /app/model/model.joblib --target-mb 500
"""
import argparse
import os
import pickle
import time

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--target-mb", type=float, required=True)
    ap.add_argument("--samples", type=int, default=200_000, help="training rows per tree")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    rng = np.random.default_rng(a.seed)
    n = a.samples
    price = rng.gamma(2.0, 25.0, n) + 1
    quantity = rng.integers(1, 6, n).astype(float)
    day = rng.integers(0, 365, n).astype(float)
    X = np.column_stack([price, quantity, day])
    y = price * quantity * (1 + 0.1 * np.sin(day / 58)) + rng.normal(0, 5, n)

    t0 = time.time()
    model = RandomForestRegressor(n_estimators=0, warm_start=True, random_state=a.seed, n_jobs=1)
    size = 0
    while size < a.target_mb * 2**20:
        model.n_estimators += 1
        model.fit(X, y)
        size += len(pickle.dumps(model.estimators_[-1], protocol=pickle.HIGHEST_PROTOCOL))
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    joblib.dump(model, a.out)  # uncompressed: loading it is the start-up work we want to measure
    mb = os.path.getsize(a.out) / 2**20
    print(f"model: {model.n_estimators} trees, {mb:.1f} MB, built in {time.time() - t0:.1f} s -> {a.out}")


if __name__ == "__main__":
    main()
