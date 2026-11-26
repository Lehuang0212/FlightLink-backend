"""OpenAPI contract drift checker comparing `docs/api/openapi.yaml` against live FastAPI routes."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

from flightlink.api.app import create_app
from flightlink.config import Settings


def _normalize_path(path: str) -> str:
    if path.startswith("/api/v1"):
        return path[len("/api/v1") :]
    return path


def check_openapi_drift(spec_path: Path) -> list[str]:
    """Compare documented `docs/api/openapi.yaml` with FastAPI's generated OpenAPI 3.1 schema.

    Returns a list of drift diagnostics (empty list == zero drift).
    """
    errors: list[str] = []
    doc = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return ["Invalid OpenAPI YAML document"]

    with tempfile.TemporaryDirectory(prefix="flightlink-openapi-") as tmp:
        app = create_app(settings=Settings(data_dir=Path(tmp)))
        live = app.openapi()

    doc_paths: dict[str, Any] = doc.get("paths", {})
    live_paths: dict[str, Any] = {_normalize_path(k): v for k, v in live.get("paths", {}).items()}

    doc_keys = set(doc_paths.keys())
    live_keys = set(live_paths.keys())
    for missing in sorted(live_keys - doc_keys):
        errors.append(f"Route present in FastAPI but missing in docs/api/openapi.yaml: {missing}")
    for extra in sorted(doc_keys - live_keys):
        errors.append(f"Route documented in docs/api/openapi.yaml but missing in FastAPI: {extra}")

    http_methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    for path in sorted(doc_keys & live_keys):
        doc_item = doc_paths[path]
        live_item = live_paths[path]
        doc_methods = {m for m in doc_item.keys() if m.lower() in http_methods}
        live_methods = {m for m in live_item.keys() if m.lower() in http_methods}
        if doc_methods != live_methods:
            errors.append(f"Method mismatch on {path}: docs={sorted(doc_methods)} vs live={sorted(live_methods)}")
            continue
        for method in sorted(doc_methods):
            d_op = doc_item[method]
            l_op = live_item[method]
            if d_op.get("operationId") != l_op.get("operationId"):
                errors.append(
                    f"operationId mismatch on {method.upper()} {path}: "
                    f"docs={d_op.get('operationId')} vs live={l_op.get('operationId')}"
                )
            # Check query/path parameter names align
            doc_params = {
                p.get("name")
                for p in (doc_item.get("parameters", []) + d_op.get("parameters", []))
                if isinstance(p, dict) and p.get("in") in ("query", "path")
            }
            # Resolve $ref path params from components.parameters
            comp_params = doc.get("components", {}).get("parameters", {})
            for p in doc_item.get("parameters", []) + d_op.get("parameters", []):
                if isinstance(p, dict) and "$ref" in p:
                    ref_name = str(p["$ref"]).rsplit("/", 1)[-1]
                    ref_obj = comp_params.get(ref_name, {})
                    if ref_obj.get("in") in ("query", "path"):
                        doc_params.add(ref_obj.get("name"))
            live_params = {
                p.get("name")
                for p in l_op.get("parameters", [])
                if isinstance(p, dict) and p.get("in") in ("query", "path")
            }
            if doc_params != live_params:
                errors.append(
                    f"Query/Path parameter mismatch on {method.upper()} {path}: "
                    f"docs={sorted(doc_params)} vs live={sorted(live_params)}"
                )

    return errors


def main() -> int:
    spec_path = Path("docs/api/openapi.yaml")
    errors = check_openapi_drift(spec_path)
    if errors:
        print("OpenAPI drift detected:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"OpenAPI contract verified with zero drift: {spec_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
