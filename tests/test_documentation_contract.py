from __future__ import annotations

import unittest
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]


class DocumentationContractTests(unittest.TestCase):
    def test_readme_has_chinese_and_english_aliyun_configuration_sections(self) -> None:
        readme = (BACKEND_ROOT / "README.md").read_text(encoding="utf-8")
        required = (
            "## 阿里云安全组配置",
            "## Alibaba Cloud security-group configuration",
            "FLIGHTLINK_SECURITY_GROUP_PROVIDER",
            "FLIGHTLINK_ALIYUN_REGION_ID",
            "FLIGHTLINK_ALIYUN_SECURITY_GROUP_ID",
            "FLIGHTLINK_ALIYUN_ECS_ROLE_NAME",
            "FLIGHTLINK_ALIYUN_UAV_SOURCE_CIDRS",
            "FLIGHTLINK_ALIYUN_GCS_SOURCE_CIDRS",
            "FLIGHTLINK_ALIYUN_API_TIMEOUT_SECONDS",
            "ecs:DescribeSecurityGroupAttribute",
            "ecs:AuthorizeSecurityGroup",
            "ecs:RevokeSecurityGroup",
            '"Resource": "*"',
            "实例 RAM 角色",
            "Instance RAM Role",
            "GET /api/v1/integrations/aliyun/security-group/preflight",
            "does not verify write permissions",
            "apt-get",
            "dnf install",
            "5761",
            "14553",
            "fake",
            "目标 ECS 上按顺序验收",
        )
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, readme)

    def test_http_examples_use_admin_session_and_document_preflight_and_channel_crud(self) -> None:
        http_examples = (BACKEND_ROOT / "test_main.http").read_text(encoding="utf-8")
        required = (
            "POST {{host}}/api/v1/auth/login",
            "@sessionToken =",
            "Cookie: flightlink_session={{sessionToken}}",
            "GET {{host}}/api/v1/integrations/aliyun/security-group/preflight",
            '"uav_udp_port": 5760',
            '"ground_station_port": 14552',
            "PUT {{host}}/api/v1/channels/{{channelId}}",
            '"uav_udp_port": 5762',
            '"ground_station_port": 14554',
            "DELETE {{host}}/api/v1/channels/{{channelId}}",
        )
        for item in required:
            with self.subTest(item=item):
                self.assertIn(item, http_examples)


if __name__ == "__main__":
    unittest.main()
