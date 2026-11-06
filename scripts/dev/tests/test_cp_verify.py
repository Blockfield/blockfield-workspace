"""Pin the frozen CP v2 wire contract and the verify harness (unit CP-S7b).

The harness must stay green on the fixtures, and any drift — an invented
route, a smuggled actor/role/via body field, or MCP reaching past the §7 API —
must turn it red. Contract source: control-plane origin/main `6df1f95`
(v2 core, PR #15); `cf660e4` (#17) touches U1 only.
"""

import copy
import json
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
DEV = TESTS.parent
sys.path.insert(0, str(DEV))
from cp import client, contract, verify  # noqa: E402

FIXTURE = TESTS / "fixtures" / "cp-v2-openapi.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class ContractPinTest(unittest.TestCase):
    def test_fixture_exists_and_is_openapi(self):
        doc = load_fixture()
        self.assertEqual(doc["openapi"], "3.1.0")
        self.assertIn("paths", doc)
        self.assertEqual(
            doc["info"]["x-blockfield-contract-sha"], contract.CONTRACT_SHA
        )

    def test_route_inventory_matches_pin(self):
        doc = load_fixture()
        found = {
            f"{method.upper()} {path}"
            for path, item in doc["paths"].items()
            for method in item
            if method in ("get", "post", "patch", "delete", "put")
        }
        self.assertEqual(found, set(contract.ROUTES))

    def test_mutation_schemas_forbid_actor_fields(self):
        doc = load_fixture()
        schemas = doc["components"]["schemas"]
        for name in contract.MUTATION_SCHEMAS:
            self.assertFalse(
                schemas[name].get("properties", {}).keys()
                & frozenset(contract.FORBIDDEN_BODY_FIELDS),
                name,
            )
            self.assertFalse(schemas[name].get("additionalProperties", True), name)

    def test_role_field_only_as_lower_grant_value(self):
        doc = load_fixture()
        schemas = doc["components"]["schemas"]
        grant_role = schemas["GrantChange"]["properties"]["role"]
        self.assertNotIn("owner", grant_role.get("enum", []))
        for name in contract.MUTATION_SCHEMAS:
            if name == contract.ROLE_FIELD_OWNER:
                continue
            self.assertNotIn("role", schemas[name].get("properties", {}), name)

    def test_allowed_action_kinds_pinned(self):
        self.assertEqual(
            list(contract.ACTION_KINDS), ["moderation.ban", "moderation.kick"]
        )


class HarnessTest(unittest.TestCase):
    def test_harness_green_on_fixture(self):
        self.assertEqual(verify.main([str(FIXTURE)]), 0)

    def test_harness_rejects_invented_route(self):
        doc = load_fixture()
        doc["paths"]["/v2/staff/grant"] = copy.deepcopy(
            doc["paths"]["/v2/staff/grants"]
        )
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_smuggled_actor_field(self):
        doc = load_fixture()
        doc["components"]["schemas"]["Mutation"]["properties"]["actor"] = {
            "type": "string"
        }
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_missing_pin(self):
        doc = load_fixture()
        del doc["info"]["x-blockfield-contract-sha"]
        self.assertNotEqual(verify.check(doc), [])


class ClientTest(unittest.TestCase):
    def test_client_refuses_actor_fields(self):
        with self.assertRaises(ValueError):
            client.build_request(
                "PATCH",
                "/v2/settings/gameplay/game",
                {"expectedRevision": 3, "idempotencyKey": "k", "actor": "x"},
            )

    def test_client_builds_minimal_mutation(self):
        request = client.build_request(
            "POST",
            "/v2/actions/moderation.kick",
            {
                "idempotencyKey": "k",
                "environment": "prod",
                "backend": "game",
                "consumerId": "game-1",
                "generation": "00000000-0000-0000-0000-000000000000",
                "target": "room-1",
                "playerId": "00000000-0000-0000-0000-000000000000",
            },
        )
        self.assertEqual(request["method"], "POST")
        self.assertNotIn("actor", request["body"])


class McpBoundaryTest(unittest.TestCase):
    def test_mcp_consumes_section7_api_only(self):
        offenders = []
        for path in sorted((DEV / "mcp").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            for marker in contract.FORBIDDEN_MCP_MARKERS:
                if marker in text:
                    offenders.append(f"{path.name}: {marker}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
