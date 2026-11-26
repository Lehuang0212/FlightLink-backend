"""History archive browsing, incremental sync manifest, authenticated file read, and CLI download router."""

from __future__ import annotations

from pathlib import Path as FsPath
from typing import Iterator, Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse

from flightlink.api.deps import AppContainer, get_container, get_current_user
from flightlink.archive.storage import build_session_bundle_tgz
from flightlink.schemas.archive import (
    ArchiveChangesResponse,
    ArchiveEntryPageResponse,
    ArchiveGroupListResponse,
    ArchiveGroupSummary,
    ArchiveSessionDetail,
    ArchiveSessionListResponse,
)
from flightlink.schemas.auth import UserProfile

router = APIRouter(prefix="/archive", tags=["Archive"])


def _stream_file_with_lease(
    path: FsPath,
    *,
    release_cb: object,
    cleanup_tmp: bool = False,
) -> Iterator[bytes]:
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                yield chunk
    finally:
        if callable(release_cb):
            release_cb()
        if cleanup_tmp:
            try:
                path.unlink()
            except OSError:
                pass


@router.get(
    "/groups",
    operation_id="listArchiveGroups",
    response_model=ArchiveGroupListResponse,
    summary="列出历史归档端口组（含活动空态组与已删组）",
)
def list_archive_groups(
    include_deleted: bool = Query(default=True),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ArchiveGroupListResponse:
    return container.archive.list_archive_groups(include_deleted=include_deleted)


@router.get(
    "/groups/{group_id}",
    operation_id="getArchiveGroup",
    response_model=ArchiveGroupSummary,
    summary="获取指定端口组的归档元数据（已删组仍返回 200）",
)
def get_archive_group(
    group_id: str = Path(...),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ArchiveGroupSummary:
    return container.archive.get_archive_group(group_id)


@router.get(
    "/groups/{group_id}/sessions",
    operation_id="listArchiveGroupSessions",
    response_model=ArchiveSessionListResponse,
    summary="分页列出某端口组的会话切片元数据（不含正文）",
)
def list_archive_group_sessions(
    group_id: str = Path(...),
    status: Literal["all", "active", "sealed"] = Query(default="all"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ArchiveSessionListResponse:
    return container.archive.list_group_sessions(
        group_id,
        status=status,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/sessions/{session_id}",
    operation_id="getArchiveSession",
    response_model=ArchiveSessionDetail,
    summary="获取单个会话切片元数据、来源可用性、缺口与文件清单（不含正文）",
)
def get_archive_session(
    session_id: str = Path(...),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ArchiveSessionDetail:
    return container.archive.get_session_detail(session_id)


@router.get(
    "/sessions/{session_id}/entries",
    operation_id="getArchiveSessionEntries",
    response_model=ArchiveEntryPageResponse,
    summary="分页浏览会话切片单源日志正文",
)
def get_archive_session_entries(
    session_id: str = Path(...),
    source: Literal["router", "mavproxy"] = Query(...),
    level: Literal["all", "DEBUG", "INFO", "WARN", "ERROR"] = Query(default="all"),
    q: str | None = Query(default=None, max_length=160),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ArchiveEntryPageResponse:
    return container.archive.get_session_entries(
        session_id,
        source=source,
        level=level,
        q=q,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/changes",
    operation_id="listArchiveChanges",
    response_model=ArchiveChangesResponse,
    summary="归档增量同步清单（仅列出已封存切片元数据）",
)
def list_archive_changes(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ArchiveChangesResponse:
    return container.archive.list_archive_changes(cursor=cursor, limit=limit)


@router.get(
    "/sessions/{session_id}/files/{file_id}",
    operation_id="readArchiveSessionFile",
    summary="鉴权读取已封存切片的单个归档文件（自动保存与手动补下载主接口）",
)
def read_archive_session_file(
    session_id: str = Path(...),
    file_id: str = Path(...),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    lease_id = container.archive.acquire_download_lease(session_id)
    try:
        disk_path, meta = container.archive.resolve_session_file(
            session_id=session_id,
            file_id=file_id,
        )
        size = disk_path.stat().st_size
    except Exception:
        container.archive.release_download_lease(lease_id)
        raise

    headers = {
        "Content-Length": str(size),
        "ETag": f'"sha256:{meta["sha256"]}"',
        "X-Content-SHA256": meta["sha256"],
        "X-Archive-Session-Id": session_id,
        "X-Archive-File-Id": meta["file_id"],
        "X-Archive-Source": meta["source"],
        "X-Archive-Expires-At": meta["expires_at"],
        "Content-Disposition": f'attachment; filename="{meta["logical_name"]}"',
    }
    return StreamingResponse(
        _stream_file_with_lease(
            disk_path,
            release_cb=lambda: container.archive.release_download_lease(lease_id),
        ),
        media_type=meta["content_type"],
        headers=headers,
    )


@router.get(
    "/sessions/{session_id}/download",
    operation_id="downloadArchiveSession",
    summary="按来源下载切片文件或打包归档（CLI 与兼容用途，非桌面端常规按钮）",
)
def download_archive_session(
    session_id: str = Path(...),
    source: Literal["router", "mavproxy", "metadata", "all"] = Query(...),
    _: UserProfile = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> StreamingResponse:
    lease_id = container.archive.acquire_download_lease(session_id)
    try:
        files, meta = container.archive.resolve_session_download_by_source(
            session_id=session_id,
            source=source,
        )
        if source == "all":
            bundle_path, digest, size = build_session_bundle_tgz(
                session_id=session_id,
                files=files,
            )
            headers = {
                "Content-Length": str(size),
                "X-Content-SHA256": digest,
                "X-Archive-Expires-At": meta["expires_at"],
                "Content-Disposition": f'attachment; filename="flightlink-{session_id}-all.tar.gz"',
            }
            return StreamingResponse(
                _stream_file_with_lease(
                    bundle_path,
                    release_cb=lambda: container.archive.release_download_lease(lease_id),
                    cleanup_tmp=True,
                ),
                media_type="application/gzip",
                headers=headers,
            )

        logical_name, disk_path = files[0]
        size = disk_path.stat().st_size
        headers = {
            "Content-Length": str(size),
            "X-Content-SHA256": meta["sha256"],
            "X-Archive-Expires-At": meta["expires_at"],
            "Content-Disposition": f'attachment; filename="flightlink-{session_id}-{logical_name}"',
        }
        return StreamingResponse(
            _stream_file_with_lease(
                disk_path,
                release_cb=lambda: container.archive.release_download_lease(lease_id),
            ),
            media_type=meta["content_type"],
            headers=headers,
        )
    except Exception:
        container.archive.release_download_lease(lease_id)
        raise
