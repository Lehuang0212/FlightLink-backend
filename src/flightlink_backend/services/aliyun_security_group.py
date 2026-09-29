from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Protocol

from ..config import Settings, settings
from ..schemas.security_group import (
    SecurityGroupPreflightPublic,
    SecurityGroupRulePublic,
)


@dataclass(frozen=True, slots=True)
class IngressRule:
    protocol: str
    port: int
    source_cidr: str
    description: str


@dataclass(frozen=True, slots=True)
class SecurityGroupRule:
    rule_id: str
    protocol: str
    port_range: str
    source_cidr: str | None
    policy: str
    description: str | None
    direction: str


@dataclass(frozen=True, slots=True)
class SecurityGroupSnapshot:
    region_id: str
    security_group_id: str
    security_group_name: str | None
    rules: list[SecurityGroupRule]


class SecurityGroupProvider(Protocol):
    def describe_security_group(self) -> SecurityGroupSnapshot: ...

    def authorize_rule(self, rule: IngressRule) -> None: ...

    def revoke_rule(self, rule_id: str) -> None: ...


class DisabledSecurityGroupProvider:
    enabled = False

    def describe_security_group(self) -> SecurityGroupSnapshot:
        raise RuntimeError("Aliyun security-group integration is disabled")

    def authorize_rule(self, rule: IngressRule) -> None:
        raise RuntimeError("Aliyun security-group integration is disabled")

    def revoke_rule(self, rule_id: str) -> None:
        raise RuntimeError("Aliyun security-group integration is disabled")


def _read_value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        if name in value:
            return value[name]
        camel_name = "".join(part.title() for part in name.split("_"))
        return value.get(camel_name, default)
    if hasattr(value, name):
        return getattr(value, name)
    camel_name = "".join(part.title() for part in name.split("_"))
    return getattr(value, camel_name, default)


@lru_cache(maxsize=8)
def _create_ecs_sdk_client(region_id: str, role_name: str | None) -> Any:
    """Build one refreshable SDK client per ECS region and instance-role name."""
    from alibabacloud_credentials.client import Client as CredentialClient
    from alibabacloud_credentials.models import Config as CredentialConfig
    from alibabacloud_ecs20140526.client import Client as Ecs20140526Client
    from alibabacloud_tea_openapi.models import Config as OpenApiConfig

    credential_values: dict[str, Any] = {"type": "ecs_ram_role"}
    if role_name:
        credential_values["role_name"] = role_name
    credentials = CredentialClient(CredentialConfig(**credential_values))
    return Ecs20140526Client(
        OpenApiConfig(
            credential=credentials,
            endpoint=f"ecs.{region_id}.aliyuncs.com",
        )
    )


class AliyunEcsSecurityGroupProvider:
    enabled = True

    def __init__(
        self,
        *,
        region_id: str | None,
        security_group_id: str | None,
        role_name: str | None,
        api_client: Any | None = None,
        api_models: Any | None = None,
        runtime_options_factory: Callable[[], Any] | None = None,
        api_timeout_seconds: int = 10,
        missing_configuration: list[str] | None = None,
    ) -> None:
        self.region_id = region_id
        self.security_group_id = security_group_id
        self.role_name = role_name
        self.api_timeout_seconds = api_timeout_seconds
        self.missing_configuration = list(missing_configuration or [])
        if not self.missing_configuration:
            if not region_id:
                self.missing_configuration.append("FLIGHTLINK_ALIYUN_REGION_ID")
            if not security_group_id:
                self.missing_configuration.append(
                    "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID"
                )
        self._api_client = api_client
        self._api_models = api_models
        self._runtime_options_factory = runtime_options_factory

    def _sdk_client(self) -> Any:
        if self._api_client is None:
            if not self.region_id:
                raise RuntimeError("Aliyun region is not configured")
            self._api_client = _create_ecs_sdk_client(self.region_id, self.role_name)
        return self._api_client

    def _sdk_models(self) -> Any:
        if self._api_models is None:
            from alibabacloud_ecs20140526 import models as ecs_20140526_models

            self._api_models = ecs_20140526_models
        return self._api_models

    def _runtime_options(self) -> Any:
        if self._runtime_options_factory is None:
            from alibabacloud_tea_util import models as util_models

            runtime = util_models.RuntimeOptions()
        else:
            runtime = self._runtime_options_factory()
        timeout_ms = self.api_timeout_seconds * 1000
        for name in ("connect_timeout", "read_timeout"):
            try:
                setattr(runtime, name, timeout_ms)
            except (AttributeError, TypeError):
                pass
        return runtime

    def _require_target(self) -> tuple[str, str]:
        if not self.region_id or not self.security_group_id:
            raise RuntimeError("Aliyun security-group target is not configured")
        return self.region_id, self.security_group_id

    def describe_security_group(self) -> SecurityGroupSnapshot:
        region_id, security_group_id = self._require_target()
        client = self._sdk_client()
        request_type = self._sdk_models().DescribeSecurityGroupAttributeRequest
        next_token: str | None = None
        seen_tokens: set[str] = set()
        rules: list[SecurityGroupRule] = []
        security_group_name: str | None = None

        while True:
            request_values: dict[str, Any] = {
                "region_id": region_id,
                "security_group_id": security_group_id,
                "direction": "ingress",
                "max_results": 1000,
            }
            if next_token:
                request_values["next_token"] = next_token
            request = request_type(**request_values)
            response = client.describe_security_group_attribute_with_options(
                request, self._runtime_options()
            )
            body = _read_value(response, "body", response)
            security_group_name = _read_value(
                body, "security_group_name", security_group_name
            )
            permissions = _read_value(body, "permissions")
            entries = _read_value(permissions, "permission", []) or []
            if not isinstance(entries, (list, tuple)):
                entries = [entries]
            for entry in entries:
                direction = str(_read_value(entry, "direction", "ingress") or "ingress")
                if direction.lower() != "ingress":
                    continue
                rules.append(
                    SecurityGroupRule(
                        rule_id=str(_read_value(entry, "security_group_rule_id", "") or ""),
                        protocol=str(_read_value(entry, "ip_protocol", "") or ""),
                        port_range=str(_read_value(entry, "port_range", "") or ""),
                        source_cidr=_read_value(entry, "source_cidr_ip"),
                        policy=str(_read_value(entry, "policy", "") or ""),
                        description=_read_value(entry, "description"),
                        direction=direction,
                    )
                )
            token = _read_value(body, "next_token")
            if not token:
                break
            token = str(token)
            if token in seen_tokens:
                raise RuntimeError("Aliyun security-group pagination token repeated")
            seen_tokens.add(token)
            next_token = token

        return SecurityGroupSnapshot(
            region_id=region_id,
            security_group_id=security_group_id,
            security_group_name=security_group_name,
            rules=rules,
        )

    def authorize_rule(self, rule: IngressRule) -> None:
        region_id, security_group_id = self._require_target()
        models = self._sdk_models()
        permission = models.AuthorizeSecurityGroupRequestPermissions(
            ip_protocol=rule.protocol.upper(),
            port_range=f"{rule.port}/{rule.port}",
            source_cidr_ip=rule.source_cidr,
            policy="Accept",
            description=rule.description,
        )
        request = self._sdk_models().AuthorizeSecurityGroupRequest(
            region_id=region_id,
            security_group_id=security_group_id,
            permissions=[permission],
        )
        self._sdk_client().authorize_security_group_with_options(
            request, self._runtime_options()
        )

    def revoke_rule(self, rule_id: str) -> None:
        region_id, security_group_id = self._require_target()
        request = self._sdk_models().RevokeSecurityGroupRequest(
            region_id=region_id,
            security_group_id=security_group_id,
            security_group_rule_id=[rule_id],
        )
        self._sdk_client().revoke_security_group_with_options(
            request, self._runtime_options()
        )


def get_security_group_provider() -> SecurityGroupProvider:
    if settings.security_group_provider == "disabled":
        return DisabledSecurityGroupProvider()

    missing_configuration: list[str] = []
    for env_name, value in (
        ("FLIGHTLINK_ALIYUN_REGION_ID", settings.aliyun_region_id),
        ("FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID", settings.aliyun_security_group_id),
        ("FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS", settings.aliyun_gcs_source_cidrs),
    ):
        if not value:
            missing_configuration.append(env_name)

    return AliyunEcsSecurityGroupProvider(
        region_id=settings.aliyun_region_id,
        security_group_id=settings.aliyun_security_group_id,
        role_name=settings.aliyun_ecs_role_name,
        api_timeout_seconds=settings.aliyun_api_timeout_seconds,
        missing_configuration=missing_configuration,
    )


def build_security_group_preflight(
    provider: SecurityGroupProvider,
) -> SecurityGroupPreflightPublic:
    if isinstance(provider, DisabledSecurityGroupProvider):
        return SecurityGroupPreflightPublic(state="disabled")

    missing_configuration = list(
        getattr(provider, "missing_configuration", []) or []
    )
    if missing_configuration:
        return SecurityGroupPreflightPublic(
            state="not_configured",
            region_id=getattr(provider, "region_id", None),
            security_group_id=getattr(provider, "security_group_id", None),
            missing_configuration=missing_configuration,
        )

    try:
        snapshot = provider.describe_security_group()
    except Exception:
        return SecurityGroupPreflightPublic(
            state="error",
            region_id=getattr(provider, "region_id", None),
            security_group_id=getattr(provider, "security_group_id", None),
            error=(
                "Unable to query the configured Aliyun security group. Check the "
                "ECS RAM role, region, security-group ID, and read permission."
            ),
        )

    return SecurityGroupPreflightPublic(
        state="ready",
        read_access_verified=True,
        write_access_checked=False,
        region_id=snapshot.region_id,
        security_group_id=snapshot.security_group_id,
        security_group_name=snapshot.security_group_name,
        ingress_rules=[
            SecurityGroupRulePublic(
                rule_id=rule.rule_id,
                protocol=rule.protocol,
                port_range=rule.port_range,
                source_cidr=rule.source_cidr,
                policy=rule.policy,
                description=rule.description,
                direction=rule.direction,
            )
            for rule in snapshot.rules
            if rule.direction.lower() == "ingress"
        ],
    )
