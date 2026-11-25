"""Instruction-following solver for DARE-Bench tabular v1 tasks.

Uses only agent-visible files (train_v1.*, val_v1.*). The pipeline parameters are the
ones the task instruction states; spec.json carries them in machine-readable form.
"""
import json
import sqlite3
import sys

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Lasso, LinearRegression, LogisticRegression, Ridge
from sklearn.multioutput import MultiOutputClassifier
from sklearn.naive_bayes import GaussianNB
from sklearn.neural_network import MLPClassifier, MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.svm import LinearSVC
from sklearn.tree import DecisionTreeClassifier, DecisionTreeRegressor


def join_sqlite(path):
    conn = sqlite3.connect(path)
    tables = pd.read_sql_query("SELECT name FROM sqlite_master WHERE type='table';", conn)["name"].tolist()
    df = None
    for table in tables:
        part = pd.read_sql_query(f"SELECT * FROM '{table}'", conn)
        if "row_id" not in part.columns:
            continue
        df = part if df is None else df.merge(part, on="row_id", how="inner")
    conn.close()
    return df.replace({None: np.nan})


def load(stem, file_type):
    if file_type == "sqlite":
        return join_sqlite(f"{stem}.sqlite")
    if file_type == "csv":
        return pd.read_csv(f"{stem}.csv", keep_default_na=False, na_values=[""])
    return pd.read_parquet(f"{stem}.parquet").replace({None: np.nan})


def apply_ranges(df, ranges):
    for col, bounds in ranges.items():
        values = df[col]
        if bounds and all(isinstance(b, bool) for b in bounds):
            # Boolean columns may be stored as 0/1 (or "0"/"1"); True/False compare equal to 1/0.
            numeric = pd.to_numeric(values, errors="coerce")
            as_text = values.astype(str).str.lower()
            ok = numeric.isin([int(b) for b in bounds]) | as_text.isin([str(b).lower() for b in bounds])
            bad = values.notna() & ~ok
        elif bounds and all(isinstance(b, (int, float)) for b in bounds):
            numeric = pd.to_numeric(values, errors="coerce")
            bad = values.notna() & ~numeric.between(bounds[0], bounds[1])
        else:
            bad = values.notna() & ~values.isin(bounds)
        df = df[~bad]
    return df


def build_model(spec):
    kind, name, rs, n_targets = spec["problem_type"], spec["model_type"], spec["random_state"], len(spec["target"])
    if kind == "classification":
        model = {
            "LogisticRegression": lambda: LogisticRegression(random_state=rs),
            "DecisionTreeClassifier": lambda: DecisionTreeClassifier(random_state=rs),
            "GaussianNB": GaussianNB,
            "LinearSVC": lambda: LinearSVC(random_state=rs),
            "MLPClassifier": lambda: MLPClassifier(random_state=rs),
        }[name]()
        if n_targets > 1 and name in ("LogisticRegression", "GaussianNB", "LinearSVC"):
            model = MultiOutputClassifier(model)
        return model
    return {
        "LinearRegression": LinearRegression,
        "DecisionTreeRegressor": lambda: DecisionTreeRegressor(random_state=rs),
        "Ridge": lambda: Ridge(random_state=rs),
        "Lasso": lambda: Lasso(random_state=rs),
        "MLPRegressor": lambda: MLPRegressor(random_state=rs),
    }[name]()


def main(spec_path):
    spec = json.loads(open(spec_path).read())
    train = apply_ranges(load("train_v1", spec["file_type"]), spec["expected_ranges"]).copy()
    val = load("val_v1", spec["file_type"])
    targets, features = spec["target"], spec["features"]
    train = train[train[targets].notna().all(axis=1)]
    for frame in (train, val):
        for col in spec["numeric_features"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    # Injected type errors can leave numeric-coded categoricals as strings in training only;
    # align them with the validation column's numeric dtype so categories match.
    for col in spec["categorical_features"]:
        if train[col].dtype == object and pd.api.types.is_numeric_dtype(val[col]):
            train[col] = pd.to_numeric(train[col], errors="coerce")
    numeric = [c for c in features if c in spec["numeric_features"]]
    categorical = [c for c in features if c in spec["categorical_features"]]
    transformers = []
    if numeric:
        transformers.append(("num", Pipeline([("imputer", SimpleImputer(strategy=spec["imputer"])), ("scaler", StandardScaler())]), numeric))
    if categorical:
        transformers.append(("cat", Pipeline([("imputer", SimpleImputer(strategy="most_frequent")), ("onehot", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), categorical))
    pipe = Pipeline([("preprocessor", ColumnTransformer(transformers)), ("model", build_model(spec))])
    pipe.fit(train[features], train[targets])
    pred = pd.DataFrame(pipe.predict(val[features]), columns=targets)
    pred.insert(0, "row_id", val["row_id"].values)
    pred.to_csv("prediction.csv", index=False)


if __name__ == "__main__":
    main(sys.argv[1])
