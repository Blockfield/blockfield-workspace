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
from unittest.mock import patch

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
            if method.lower() in verify.OPENAPI_METHODS
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
        enum = grant_role.get("enum")
        self.assertIsNotNone(enum, "GrantChange.role must declare an explicit enum")
        self.assertNotIn("owner", enum)
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

    def test_harness_rejects_owner_admitting_role_ref(self):
        doc = load_fixture()
        doc["components"]["schemas"]["GrantChange"]["properties"]["role"] = {
            "$ref": "#/components/schemas/Role"
        }
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_role_without_enum(self):
        doc = load_fixture()
        doc["components"]["schemas"]["GrantChange"]["properties"]["role"] = {
            "type": "string"
        }
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_unexpected_options_method(self):
        doc = load_fixture()
        doc["paths"]["/v2/staff/grants"]["options"] = {
            "operationId": "grantOptions",
            "responses": {"200": {"description": "invented"}},
        }
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_uppercase_invented_method(self):
        doc = load_fixture()
        doc["paths"]["/v2/staff/grant"] = {
            "x-blockfield-provenance": "cpv2/app.py@6df1f95:446",
            "POST": {
                "operationId": "grantTypo",
                "responses": {"200": {"description": "invented"}},
            },
        }
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_unknown_path_item_key(self):
        doc = load_fixture()
        doc["paths"]["/v2/staff/grants"]["summary2"] = "invented key"
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_mutating_route_without_body(self):
        doc = load_fixture()
        doc["paths"]["/v2/staff/grants"]["post"].pop("requestBody", None)
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_inline_owner_body(self):
        doc = load_fixture()
        doc["paths"]["/v2/staff/grants"]["post"]["requestBody"] = {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"role": {"enum": ["admin", "owner"]}},
                    }
                }
            }
        }
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_empty_additional_properties(self):
        doc = load_fixture()
        doc["components"]["schemas"]["Mutation"]["additionalProperties"] = {}
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_missing_additional_properties(self):
        doc = load_fixture()
        del doc["components"]["schemas"]["Mutation"]["additionalProperties"]
        self.assertNotEqual(verify.check(doc), [])

    def test_harness_rejects_grantchange_without_role(self):
        doc = load_fixture()
        del doc["components"]["schemas"]["GrantChange"]["properties"]["role"]
        self.assertNotEqual(verify.check(doc), [])


class FakeConnection:
    """http.client stand-in without the context-manager protocol."""

    last = None

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.sent = {}
        self.closed = False
        FakeConnection.last = self

    def request(self, method, url, body=None, headers=None):
        self.sent = {
            "method": method,
            "url": url,
            "body": body,
            "headers": headers,
        }

    def getresponse(self):
        body = json.dumps({"ok": True}).encode("utf-8")

        class Response:
            status = 200

            @staticmethod
            def read():
                return body

        return Response()

    def close(self):
        self.closed = True


def send_through_fake(base_url, body):
    request = client.build_request("POST", "/v2/actions/moderation.kick", body)
    with (
        patch("http.client.HTTPConnection", FakeConnection),
        patch("http.client.HTTPSConnection", FakeConnection),
    ):
        status, parsed = client.send(base_url, request, "bearer-1")
    return status, parsed, FakeConnection.last


class ClientTest(unittest.TestCase):
    def test_client_refuses_actor_fields(self):
        with self.assertRaises(ValueError):
            client.build_request(
                "PATCH",
                "/v2/settings/gameplay/game",
                {"expectedRevision": 3, "idempotencyKey": "k", "actor": "x"},
            )

    def test_send_completes_request_and_closes(self):
        status, parsed, connection = send_through_fake(
            "https://cp.internal.example",
            {"idempotencyKey": "k", "reason": "spam"},
        )
        self.assertEqual((status, parsed), (200, {"ok": True}))
        self.assertEqual(connection.sent["method"], "POST")
        self.assertTrue(connection.closed, "connection must be closed")

    def test_send_encodes_non_ascii_body_as_utf8(self):
        _, _, connection = send_through_fake(
            "https://cp.internal.example",
            {"idempotencyKey": "k", "reason": "spam — repeat café"},
        )
        body = connection.sent["body"]
        self.assertIsInstance(body, bytes)
        self.assertEqual(
            json.loads(body.decode("utf-8"))["reason"], "spam — repeat café"
        )
        self.assertIn("charset=utf-8", connection.sent["headers"]["Content-Type"])

    def test_send_refuses_cleartext_bearer_off_loopback(self):
        with self.assertRaises(ValueError):
            send_through_fake("http://cp.internal.example", {"idempotencyKey": "k"})

    def test_send_allows_http_only_on_loopback(self):
        for base_url in (
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "http://[::1]:8080",
        ):
            status, _, connection = send_through_fake(base_url, {"idempotencyKey": "k"})
            self.assertEqual(status, 200)
            self.assertTrue(connection.closed)

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
        offenders, scanned = verify.check_mcp(DEV / "mcp")
        self.assertGreaterEqual(scanned, 1, "boundary test scanned nothing")
        self.assertEqual(offenders, [])

    def test_mcp_scan_finds_nested_marker(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "pkg"
            nested.mkdir()
            (nested / "bridge.py").write_text(
                "TOKEN = BLOCKFIELD_CP_GATE_WRITE_TOKEN", encoding="utf-8"
            )
            offenders, scanned = verify.check_mcp(Path(tmp))
            self.assertGreaterEqual(scanned, 1)
            self.assertEqual(len(offenders), 1)


if __name__ == "__main__":
    unittest.main()
