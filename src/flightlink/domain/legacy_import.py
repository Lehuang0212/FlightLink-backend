"""Read-only migration importer for legacy `old-version-0.1` SQLite databases.

Guarantees:
- Opens legacy DB strictly in read-only mode (`mode=ro`).
- Refuses to import if legacy services are reported active (prevents old & new
  services from managing the same ports or cloud rules simultaneously).
- Imported groups always start in `configured_stopped` (`desired_state='stopped'`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from flightlink.domain.errors import ConflictError
from flightlink.domain.operations_repo import ControlPlaneRepository
from flightlink.schemas.groups import CreateGroupRequest


def import_legacy_v01_readonly(
    *,
    legacy_db_path: Path,
    repo: ControlPlaneRepository,
    requested_by: str = "legacy-import",
    legacy_service_active: bool = False,
) -> dict[str, Any]:
    if legacy_service_active:
        raise ConflictError(
            "LEGACY_SERVICE_ACTIVE",
            "Stop legacy FlightLink 0.1 services before importing so old and new services never manage the same ports or cloud rules.",
        )

    if not legacy_db_path.exists():
        raise FileNotFoundError(f"Legacy database not found: {legacy_db_path}")

    uri = f"file:{legacy_db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {
            str(r["name"])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "channels" not in tables:
            return {"imported": [], "skipped": [], "reason": "No 'channels' table in legacy database"}

        rows = conn.execute("SELECT * FROM channels ORDER BY id ASC").fetchall()
    finally:
        conn.close()

    imported: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        cols = set(row.keys())
        name = str(row["name"] if "name" in cols else f"导入组-{row['id']}")[:24].strip() or f"导入组-{row['id']}"
        dtu_port = int(row["uav_port"] if "uav_port" in cols else row["dtu_port"])
        gcs_port = int(row["gcs_port"])
        gcs_proto = str(row["gcs_protocol"] if "gcs_protocol" in cols else "tcp").lower()
        if gcs_proto not in ("tcp", "udp"):
            gcs_proto = "tcp"
        try:
            req = CreateGroupRequest(
                name=name,
                dtu_port=dtu_port,
                gcs_protocol=gcs_proto,  # type: ignore[arg-type]
                gcs_port=gcs_port,
            )
            op = repo.enqueue_create_group(req, requested_by=requested_by)
            imported.append(
                {
                    "legacy_id": row["id"] if "id" in cols else None,
                    "group_id": op.group_id,
                    "operation_id": op.operation_id,
                    "name": name,
                    "dtu_port": dtu_port,
                    "gcs_protocol": gcs_proto,
                    "gcs_port": gcs_port,
                    "desired_state": "stopped",
                }
            )
        except Exception as exc:
            skipped.append({"name": name, "dtu_port": dtu_port, "gcs_port": gcs_port, "error": str(exc)})

    return {"imported": imported, "skipped": skipped}
