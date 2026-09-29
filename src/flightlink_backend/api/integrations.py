from __future__ import annotations

from fastapi import APIRouter, Depends

from ..api.auth import AdminPublic, get_current_admin
from ..schemas.security_group import SecurityGroupPreflightPublic
from ..services.aliyun_security_group import (
    SecurityGroupProvider,
    build_security_group_preflight,
    get_security_group_provider,
)

router = APIRouter(prefix="/integrations/aliyun", tags=["integrations"])


@router.get(
    "/security-group/preflight",
    response_model=SecurityGroupPreflightPublic,
    summary="Check read access to the configured Aliyun security group",
)
def read_security_group_preflight(
    _: AdminPublic = Depends(get_current_admin),
    provider: SecurityGroupProvider = Depends(get_security_group_provider),
) -> SecurityGroupPreflightPublic:
    return build_security_group_preflight(provider)
