"""Baseline solver for DARE-Bench modeling (tabular v2) and forecasting (time-series v1/v2) tasks.

Uses only agent-visible files. spec.json names the variant, targets, file type and (for
time series) the time column, all of which the instruction or metadata.txt states.
"""
import json
import sqlite3
import sys

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor


def join_sqlite(path):
    conn = sqlite3.connect(path)
    tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table';", conn)["name"].tolist()
    df = None
    for table in tables:
        part = pd.read_sql_query(f"SELECT * FROM '{table}'", conn)
        if "row_id" in part.columns:
            df = part if df is None else df.merge(part, on="row_id", how="inner")
        elif len(tables) == 1:
            df = part
    conn.close()
    return df.replace({None: np.nan})


def load(name):
    if name.endswith(".sqlite"):
        return join_sqlite(name)
    if name.endswith(".csv"):
        return pd.read_csv(name, keep_default_na=False, na_values=[""])
    return pd.read_parquet(name).replace({None: np.nan})


def featurize(train, test, drop, time_col=None, time_format=None):
    cols = [c for c in train.columns if c not in drop and c in test.columns]
    X_tr, X_te = train[cols].copy(), test[cols].copy()
    if time_col and time_col in cols:
        for X in (X_tr, X_te):
            t = pd.to_datetime(X[time_col], format=time_format, errors="coerce", utc=True).dt.tz_localize(None)
            X["_t"] = (t - pd.Timestamp("1970-01-01")).dt.total_seconds() / 86400
            X["_year"], X["_month"], X["_dow"], X["_doy"], X["_hour"] = t.dt.year, t.dt.month, t.dt.dayofweek, t.dt.dayofyear, t.dt.hour
            X.drop(columns=[time_col], inplace=True)
    cat_mask = []
    for c in X_tr.columns:
        numeric_tr = pd.to_numeric(X_tr[c], errors="coerce")
        if X_tr[c].dtype == object and numeric_tr.notna().mean() < 0.95:
            codes = {v: i for i, v in enumerate(pd.concat([X_tr[c], X_te[c]]).astype(str).unique())}
            X_tr[c], X_te[c] = X_tr[c].astype(str).map(codes), X_te[c].astype(str).map(codes)
            cat_mask.append(len(codes) <= 255)
            if len(codes) > 255:
                X_tr[c], X_te[c] = X_tr[c].astype(float), X_te[c].astype(float)
        else:
            X_tr[c], X_te[c] = numeric_tr, pd.to_numeric(X_te[c], errors="coerce")
            cat_mask.append(False)
    return X_tr.astype(float), X_te.astype(float), np.array(cat_mask, dtype=bool)


def main(spec_path):
    spec = json.loads(open(spec_path).read())
    train, test = load(spec["train_file"]), load(spec["test_file"])
    targets, kind = spec["target"], spec["problem_type"]
    time_col = spec.get("time_column")
    id_cols = [c for c in test.columns if c != "row_id"] if spec["output_ids"] == "all_test_columns" else ["row_id"]
    X_tr, X_te, cat = featurize(train, test, set(targets) | {"row_id"}, time_col, spec.get("time_format"))
    out = test[id_cols].copy()
    for t in targets:
        y = train[t]
        keep = y.notna().to_numpy()
        if kind == "classification":
            model = HistGradientBoostingClassifier(categorical_features=cat if cat.any() else None, random_state=0)
            model.fit(X_tr[keep], y[keep].astype(str))
            pred = model.predict(X_te)
            # restore the original label type where possible
            original = y[keep]
            if pd.api.types.is_numeric_dtype(original):
                pred = pd.to_numeric(pd.Series(pred), errors="coerce").to_numpy()
                if (original.dropna() % 1 == 0).all():
                    pred = pred.astype("int64")
        else:
            model = HistGradientBoostingRegressor(categorical_features=cat if cat.any() else None, random_state=0)
            model.fit(X_tr[keep], pd.to_numeric(y[keep], errors="coerce"))
            pred = model.predict(X_te)
        out[t] = pred
    out.to_csv("prediction.csv", index=False)


if __name__ == "__main__":
    main(sys.argv[1])
