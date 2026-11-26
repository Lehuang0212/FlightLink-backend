"""Aliyun ECS Instance RAM Role + Dedicated Security Group client.

Security & reliability guarantees:
- NEVER stores long-lived AccessKey credentials.
- NEVER creates IAM/RAM roles or escalates its own permissions.
- Obtains short-lived STS credentials strictly from ECS Instance Metadata Service
  (IMDSv2 token preferred, IMDSv1 fallback).
- Tracks rules by cloud `SecurityGroupRuleId` + `Description` ownership marker;
  NEVER revokes rules by port number alone so manual/SSH rules are untouchable.
- Maps network timeouts to `CloudTimeoutError` (caller queries live state first)
  and RAM/STS failures to `CloudAuthTemporaryError` (caller never restarts active channels).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

import httpx

from flightlink.adapters.interfaces import (
    CloudAuthTemporaryError,
    CloudSecurityGroupRule,
    CloudTimeoutError,
    InfrastructureError,
)
from flightlink.clock import Clock, SystemClock, parse_rfc3339

IMDS_BASE = "http://100.100.100.200/latest"


@dataclass
class RamStsCredentials:
    role_name: str
    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: datetime


def _percent_encode(value: str) -> str:
    return urllib.parse.quote(str(value), safe="-_.~")


def sign_aliyun_rpc_params(params: dict[str, str], access_key_secret: str, method: str = "POST") -> str:
    canonical = "&".join(
        f"{_percent_encode(k)}={_percent_encode(v)}"
        for k, v in sorted(params.items())
    )
    string_to_sign = f"{method}&%2F&{_percent_encode(canonical)}"
    mac = hmac.new(
        (access_key_secret + "&").encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha1,
    )
    return base64.b64encode(mac.digest()).decode("ascii")


class AliyunRamSecurityGroupClient:
    """Real Aliyun ECS OpenAPI (2014-05-26) Security Group client using ECS RAM role."""

    def __init__(
        self,
        *,
        region: str,
        ram_role_name: str | None = None,
        timeout_seconds: float = 10.0,
        clock: Clock | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.region = region
        self.ram_role_name = ram_role_name
        self.timeout_seconds = timeout_seconds
        self.clock = clock or SystemClock()
        self._client = http_client or httpx.Client(timeout=timeout_seconds)
        self._cached_creds: RamStsCredentials | None = None

    def _get_imds_headers(self) -> dict[str, str]:
        try:
            resp = self._client.put(
                f"{IMDS_BASE}/api/token",
                headers={"X-aliyun-ecs-metadata-token-ttl-seconds": "600"},
                timeout=3.0,
            )
            if resp.status_code == 200 and resp.text.strip():
                return {"X-aliyun-ecs-metadata-token": resp.text.strip()}
        except httpx.HTTPError:
            pass
        return {}

    def fetch_ram_credentials(self) -> RamStsCredentials:
        now = self.clock.now()
        if (
            self._cached_creds is not None
            and (self._cached_creds.expiration - now).total_seconds() > 120
        ):
            return self._cached_creds

        headers = self._get_imds_headers()
        role_name = self.ram_role_name
        try:
            if not role_name:
                r_list = self._client.get(
                    f"{IMDS_BASE}/meta-data/ram/security-credentials/",
                    headers=headers,
                    timeout=3.0,
                )
                if r_list.status_code != 200 or not r_list.text.strip():
                    raise CloudAuthTemporaryError("No ECS instance RAM role attached to this host.")
                role_name = r_list.text.strip().splitlines()[0].strip()

            r_cred = self._client.get(
                f"{IMDS_BASE}/meta-data/ram/security-credentials/{role_name}",
                headers=headers,
                timeout=3.0,
            )
            if r_cred.status_code != 200:
                raise CloudAuthTemporaryError(
                    f"Failed to fetch STS credentials for RAM role '{role_name}' (HTTP {r_cred.status_code})."
                )
            data = r_cred.json()
            if data.get("Code") != "Success":
                raise CloudAuthTemporaryError(f"RAM metadata error: {data.get('Code')}")
            exp = parse_rfc3339(str(data["Expiration"])) or datetime.now(timezone.utc)
            self._cached_creds = RamStsCredentials(
                role_name=role_name,
                access_key_id=str(data["AccessKeyId"]),
                access_key_secret=str(data["AccessKeySecret"]),
                security_token=str(data["SecurityToken"]),
                expiration=exp,
            )
            return self._cached_creds
        except CloudAuthTemporaryError:
            raise
        except httpx.HTTPError as exc:
            raise CloudAuthTemporaryError(f"ECS RAM metadata service unavailable: {exc}") from exc

    def _call_ecs(self, action: str, extra_params: dict[str, str]) -> dict[str, Any]:
        creds = self.fetch_ram_credentials()
        now = self.clock.now().astimezone(timezone.utc)
        params: dict[str, str] = {
            "Format": "JSON",
            "Version": "2014-05-26",
            "AccessKeyId": creds.access_key_id,
            "SecurityToken": creds.security_token,
            "SignatureMethod": "HMAC-SHA1",
            "Timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "SignatureVersion": "1.0",
            "SignatureNonce": secrets.token_hex(16),
            "Action": action,
            "RegionId": self.region,
            **extra_params,
        }
        params["Signature"] = sign_aliyun_rpc_params(params, creds.access_key_secret, method="POST")
        endpoint = f"https://ecs.{self.region}.aliyuncs.com/"
        try:
            resp = self._client.post(endpoint, data=params, timeout=self.timeout_seconds)
        except httpx.TimeoutException as exc:
            raise CloudTimeoutError(f"Aliyun ECS API '{action}' timed out") from exc
        except httpx.HTTPError as exc:
            raise CloudTimeoutError(f"Aliyun ECS API '{action}' transport error: {exc}") from exc

        try:
            body = resp.json()
        except Exception as exc:
            raise InfrastructureError("CLOUD_API_INVALID_RESPONSE", f"Invalid JSON from Aliyun ({resp.status_code})") from exc

        if resp.status_code in (401, 403) or body.get("Code") in (
            "InvalidAccessKeyId.NotFound",
            "InvalidSecurityToken.Expired",
            "Forbidden.RAM",
        ):
            self._cached_creds = None
            raise CloudAuthTemporaryError(
                f"Aliyun RAM authorization failure on {action}: {body.get('Code')} - {body.get('Message')}"
            )
        if resp.status_code >= 400 or "Code" in body:
            raise InfrastructureError(
                "CLOUD_API_ERROR",
                f"Aliyun ECS {action} error ({resp.status_code}): {body.get('Code')} - {body.get('Message')}",
            )
        return body

    def validate_connection(
        self,
        *,
        region: str,
        security_group_id: str,
        ram_role: str | None = None,
    ) -> dict[str, str]:
        self.region = region
        if ram_role:
            self.ram_role_name = ram_role
        creds = self.fetch_ram_credentials()
        data = self._call_ecs(
            "DescribeSecurityGroups",
            {"SecurityGroupId": security_group_id},
        )
        sgs = data.get("SecurityGroups", {}).get("SecurityGroup", [])
        if not sgs:
            raise InfrastructureError(
                "SECURITY_GROUP_NOT_FOUND",
                f"Security group '{security_group_id}' not found in region '{region}'.",
                retryable=False,
            )
        # Also verify DescribeSecurityGroupAttribute read permission
        self._call_ecs("DescribeSecurityGroupAttribute", {"SecurityGroupId": security_group_id})
        return {
            "region": region,
            "security_group_id": security_group_id,
            "ram_role": creds.role_name,
            "security_group_name": str(sgs[0].get("SecurityGroupName", "")),
            "status": "verified",
        }

    def list_rules(self, *, security_group_id: str) -> list[CloudSecurityGroupRule]:
        data = self._call_ecs(
            "DescribeSecurityGroupAttribute",
            {"SecurityGroupId": security_group_id, "Direction": "ingress"},
        )
        perms = data.get("Permissions", {}).get("Permission", [])
        rules: list[CloudSecurityGroupRule] = []
        for p in perms:
            proto = str(p.get("IpProtocol", "")).lower()
            if proto not in ("tcp", "udp"):
                continue
            port_range = str(p.get("PortRange", ""))
            if "/" not in port_range:
                continue
            start_s, end_s = port_range.split("/", 1)
            if not (start_s.isdigit() and end_s.isdigit() and start_s == end_s):
                continue
            rules.append(
                CloudSecurityGroupRule(
                    rule_id=str(p.get("SecurityGroupRuleId", "")),
                    security_group_id=security_group_id,
                    protocol=proto,  # type: ignore[arg-type]
                    port=int(start_s),
                    source_cidr=str(p.get("SourceCidrIp", "")),
                    description=str(p.get("Description", "")),
                )
            )
        return rules

    def authorize_rule(
        self,
        *,
        security_group_id: str,
        protocol: Literal["udp", "tcp"],
        port: int,
        source_cidr: str,
        description: str,
    ) -> CloudSecurityGroupRule:
        data = self._call_ecs(
            "AuthorizeSecurityGroup",
            {
                "SecurityGroupId": security_group_id,
                "Permissions.1.IpProtocol": protocol,
                "Permissions.1.PortRange": f"{port}/{port}",
                "Permissions.1.SourceCidrIp": source_cidr,
                "Permissions.1.Policy": "accept",
                "Permissions.1.Priority": "1",
                "Permissions.1.Description": description,
            },
        )
        rule_ids = data.get("SecurityGroupRuleIds", {}).get("SecurityGroupRuleId", [])
        rule_id = str(rule_ids[0]) if rule_ids else ""
        if not rule_id:
            # Query back to capture assigned SecurityGroupRuleId
            for lr in self.list_rules(security_group_id=security_group_id):
                if (
                    lr.protocol == protocol
                    and lr.port == port
                    and lr.source_cidr == source_cidr
                    and lr.description == description
                ):
                    return lr
        return CloudSecurityGroupRule(
            rule_id=rule_id,
            security_group_id=security_group_id,
            protocol=protocol,
            port=port,
            source_cidr=source_cidr,
            description=description,
        )

    def revoke_rule(self, *, security_group_id: str, rule_id: str) -> None:
        """Strictly revoke by exact `SecurityGroupRuleId` (NEVER by port number!)."""
        if not rule_id:
            raise InfrastructureError("MISSING_RULE_ID", "Refusing to revoke cloud rule without SecurityGroupRuleId.")
        self._call_ecs(
            "RevokeSecurityGroup",
            {
                "SecurityGroupId": security_group_id,
                "SecurityGroupRuleId.1": rule_id,
            },
        )
