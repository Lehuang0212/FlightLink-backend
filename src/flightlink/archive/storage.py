"""Atomic filesystem helpers for `/var/lib/flightlink/archive/<group_id>/<session_id>/`."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any


def file_sha256_and_size(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def write_bytes_atomic(target: Path, data: bytes) -> tuple[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp.{os.getpid()}"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, target)
    return hashlib.sha256(data).hexdigest(), len(data)


def write_json_atomic(target: Path, obj: dict[str, Any]) -> tuple[str, int]:
    raw = (json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return write_bytes_atomic(target, raw)


def append_log_line(path: Path, line: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (line.rstrip("\r\n") + "\n").encode("utf-8")
    with open(path, "ab") as fh:
        fh.write(encoded)
        fh.flush()
    return len(encoded)


def build_session_bundle_tgz(
    *,
    session_id: str,
    files: list[tuple[str, Path]],
) -> tuple[Path, str, int]:
    """Create a deterministic tar.gz archive of available session files for CLI/compat export."""
    fd, tmp_name = tempfile.mkstemp(prefix=f"flightlink-{session_id}-", suffix=".tar.gz")
    os.close(fd)
    tmp_path = Path(tmp_name)
    with tarfile.open(tmp_path, mode="w:gz") as tar:
        for arcname, src_path in sorted(files, key=lambda x: x[0]):
            if src_path.exists() and src_path.is_file():
                tar.add(src_path, arcname=f"{session_id}/{arcname}")
    digest, size = file_sha256_and_size(tmp_path)
    return tmp_path, digest, size


def safe_rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
