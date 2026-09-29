from __future__ import annotations

import importlib
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class AliyunSecurityGroupTests(unittest.TestCase):
    def load_service(self):
        try:
            return importlib.import_module(
                "flightlink_backend.services.aliyun_security_group"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"Aliyun security-group service is missing: {exc.name}")

    def test_preflight_reports_disabled_provider(self) -> None:
        service = self.load_service()
        provider_type = getattr(service, "DisabledSecurityGroupProvider", None)
        builder = getattr(service, "build_security_group_preflight", None)
        self.assertTrue(callable(provider_type))
        self.assertTrue(callable(builder))

        result = builder(provider_type())

        self.assertEqual(result.state, "disabled")
        self.assertIsNone(result.error)
        self.assertEqual(result.ingress_rules, [])

    def test_preflight_returns_target_and_all_paginated_ingress_rules(self) -> None:
        service = self.load_service()
        provider_type = getattr(service, "AliyunEcsSecurityGroupProvider", None)
        builder = getattr(service, "build_security_group_preflight", None)
        self.assertTrue(callable(provider_type))
        self.assertTrue(callable(builder))

        class FakeEcsClient:
            def __init__(self):
                self.requests = []
                self.authorize_calls = 0
                self.revoke_calls = 0

            def describe_security_group_attribute_with_options(self, request, runtime):
                self.requests.append(request)
                if getattr(request, "next_token", None) is None:
                    body = SimpleNamespace(
                        region_id="cn-hangzhou",
                        security_group_id="sg-test",
                        security_group_name="FlightLink test",
                        permissions=SimpleNamespace(
                            permission=[
                                SimpleNamespace(
                                    security_group_rule_id="sgr-1",
                                    direction="ingress",
                                    ip_protocol="udp",
                                    port_range="5760/5760",
                                    source_cidr_ip="0.0.0.0/0",
                                    policy="Accept",
                                    description="FlightLink DTU",
                                )
                            ]
                        ),
                        next_token="page-2",
                    )
                else:
                    body = SimpleNamespace(
                        region_id="cn-hangzhou",
                        security_group_id="sg-test",
                        security_group_name="FlightLink test",
                        permissions=SimpleNamespace(
                            permission=[
                                SimpleNamespace(
                                    security_group_rule_id="sgr-2",
                                    direction="ingress",
                                    ip_protocol="tcp",
                                    port_range="14553/14553",
                                    source_cidr_ip="198.51.100.7/32",
                                    policy="Accept",
                                    description="FlightLink GCS",
                                )
                            ]
                        ),
                        next_token=None,
                    )
                return SimpleNamespace(body=body)

        client = FakeEcsClient()
        provider = provider_type(
            region_id="cn-hangzhou",
            security_group_id="sg-test",
            role_name="flightlink-role",
            api_client=client,
            runtime_options_factory=lambda: object(),
            api_timeout_seconds=10,
        )

        result = builder(provider)

        self.assertEqual(result.state, "ready")
        self.assertEqual(getattr(result, "read_access_verified", None), True)
        self.assertEqual(getattr(result, "write_access_checked", None), False)
        self.assertEqual(result.region_id, "cn-hangzhou")
        self.assertEqual(result.security_group_id, "sg-test")
        self.assertEqual(result.security_group_name, "FlightLink test")
        self.assertEqual([rule.rule_id for rule in result.ingress_rules], ["sgr-1", "sgr-2"])
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.authorize_calls, 0)
        self.assertEqual(client.revoke_calls, 0)

    def test_preflight_reports_missing_configuration_without_sdk_call(self) -> None:
        service = self.load_service()
        provider_type = getattr(service, "AliyunEcsSecurityGroupProvider", None)
        builder = getattr(service, "build_security_group_preflight", None)
        self.assertTrue(callable(provider_type))
        self.assertTrue(callable(builder))

        class NeverCalledClient:
            def describe_security_group_attribute_with_options(self, request, runtime):
                raise AssertionError("SDK must not be called with incomplete settings")

        provider = provider_type(
            region_id=None,
            security_group_id=None,
            role_name=None,
            api_client=NeverCalledClient(),
            api_models=SimpleNamespace(),
            runtime_options_factory=lambda: object(),
            api_timeout_seconds=10,
            missing_configuration=[
                "FLIGHTLINK_ALIYUN_REGION_ID",
                "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID",
            ],
        )

        result = builder(provider)

        self.assertEqual(result.state, "not_configured")
        self.assertEqual(
            result.missing_configuration,
            ["FLIGHTLINK_ALIYUN_REGION_ID", "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID"],
        )
        self.assertEqual(result.ingress_rules, [])

    def test_preflight_sanitizes_credential_and_openapi_errors(self) -> None:
        service = self.load_service()
        builder = getattr(service, "build_security_group_preflight", None)
        self.assertTrue(callable(builder))

        class BrokenProvider:
            def describe_security_group(self):
                raise RuntimeError(
                    "AccessKeySecret=do-not-expose Signature=private-request-signature"
                )

        result = builder(BrokenProvider())
        public_json = result.model_dump_json()

        self.assertEqual(result.state, "error")
        self.assertIsNotNone(result.error)
        self.assertNotIn("do-not-expose", public_json)
        self.assertNotIn("private-request-signature", public_json)

    def test_sdk_adapter_maps_authorize_and_revoke_to_exact_rule_requests(self) -> None:
        service = self.load_service()
        provider_type = getattr(service, "AliyunEcsSecurityGroupProvider", None)
        rule_type = getattr(service, "IngressRule", None)
        self.assertTrue(callable(provider_type))
        self.assertTrue(callable(rule_type))

        class FakeEcsClient:
            def __init__(self):
                self.authorized = []
                self.revoked = []

            def authorize_security_group_with_options(self, request, runtime):
                self.authorized.append(request)

            def revoke_security_group_with_options(self, request, runtime):
                self.revoked.append(request)

        client = FakeEcsClient()
        provider = provider_type(
            region_id="cn-hangzhou",
            security_group_id="sg-test",
            role_name=None,
            api_client=client,
            runtime_options_factory=lambda: SimpleNamespace(),
            api_timeout_seconds=10,
        )

        provider.authorize_rule(
            rule_type(
                protocol="udp",
                port=5761,
                source_cidr="0.0.0.0/0",
                description="FlightLink test channel UAV",
            )
        )
        provider.revoke_rule("sgr-owned-rule")

        self.assertEqual(len(client.authorized), 1)
        self.assertEqual(client.authorized[0].region_id, "cn-hangzhou")
        self.assertEqual(client.authorized[0].security_group_id, "sg-test")
        permissions = getattr(client.authorized[0], "permissions", None)
        self.assertIsNotNone(permissions, "SDK must use the Permissions rule list")
        permission = permissions[0]
        self.assertEqual(permission.ip_protocol, "UDP")
        self.assertEqual(permission.port_range, "5761/5761")
        self.assertEqual(permission.source_cidr_ip, "0.0.0.0/0")
        self.assertEqual(permission.policy, "Accept")
        self.assertEqual(permission.description, "FlightLink test channel UAV")
        self.assertEqual(len(client.revoked), 1)
        self.assertEqual(client.revoked[0].region_id, "cn-hangzhou")
        self.assertEqual(client.revoked[0].security_group_id, "sg-test")
        self.assertEqual(client.revoked[0].security_group_rule_id, ["sgr-owned-rule"])

    def test_preflight_route_is_admin_protected_and_read_only(self) -> None:
        from fastapi import FastAPI
        from fastapi.dependencies.utils import get_dependant

        service = self.load_service()
        api_module = importlib.import_module("flightlink_backend.api.integrations")
        auth_module = importlib.import_module("flightlink_backend.api.auth")

        app = FastAPI()
        app.include_router(api_module.router, prefix="/api/v1")
        endpoint_path = "/integrations/aliyun/security-group/preflight"
        self.assertIn(
            f"/api/v1{endpoint_path}",
            app.openapi()["paths"],
            "read-only preflight route is not mounted",
        )
        route = next(
            route
            for route in api_module.router.routes
            if getattr(route, "path", None) == endpoint_path
        )
        dependant = get_dependant(path=route.path, call=route.endpoint)

        def dependency_calls(node):
            yield from (dependency.call for dependency in node.dependencies)
            for dependency in node.dependencies:
                yield from dependency_calls(dependency)

        self.assertIn(auth_module.get_current_admin, set(dependency_calls(dependant)))
        self.assertTrue(callable(getattr(service, "get_security_group_provider", None)))

    def test_settings_load_aliyun_values_and_dtu_source_default(self) -> None:
        from flightlink_backend.config import Settings

        values = {
            "FLIGHTLINK_SECURITY_GROUP_PROVIDER": "aliyun",
            "FLIGHTLINK_ALIYUN_REGION_ID": "cn-hangzhou",
            "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID": "sg-test",
            "FLIGHTLINK_ALIYUN_ECS_ROLE_NAME": "flightlink-role",
            "FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS": "198.51.100.7/32,203.0.113.0/24",
        }
        with patch.dict(os.environ, values, clear=True):
            configured = Settings.from_environment()

        self.assertEqual(getattr(configured, "security_group_provider", None), "aliyun")
        self.assertEqual(getattr(configured, "aliyun_region_id", None), "cn-hangzhou")
        self.assertEqual(getattr(configured, "aliyun_security_group_id", None), "sg-test")
        self.assertEqual(getattr(configured, "aliyun_ecs_role_name", None), "flightlink-role")
        self.assertEqual(
            getattr(configured, "aliyun_uav_source_cidrs", None), "0.0.0.0/0"
        )
        self.assertEqual(
            getattr(configured, "aliyun_gcs_source_cidrs", None),
            "198.51.100.7/32,203.0.113.0/24",
        )
        self.assertEqual(getattr(configured, "aliyun_api_timeout_seconds", None), 10)
        self.assertFalse(hasattr(configured, "aliyun_access_key_id"))


if __name__ == "__main__":
    unittest.main()
