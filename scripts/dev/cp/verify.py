"""Verify-harness CLI: check an OpenAPI fixture against the frozen pin.

    python scripts/dev/cp/verify.py scripts/dev/tests/fixtures/cp-v2-openapi.json

Exit 0 with one PASS line per check group; otherwise the failing reasons.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cp import contract  # noqa: E402

METHODS = ("get", "post", "patch", "delete", "put")


def check(doc):
    """Return a list of failure reasons; empty means the fixture is green."""
    failures = []
    info = doc.get("info", {})
    if info.get("x-blockfield-contract-sha") != contract.CONTRACT_SHA:
        failures.append("info.x-blockfield-contract-sha != frozen pin")
    paths = doc.get("paths", {})
    found = {
        f"{method.upper()} {path}"
        for path, item in paths.items()
        for method in item
        if method in METHODS
    }
    for route in sorted(set(contract.ROUTES) - found):
        failures.append(f"missing route: {route}")
    for route in sorted(found - set(contract.ROUTES)):
        failures.append(f"invented route (not in frozen contract): {route}")
    for path, item in paths.items():
        provenance = item.get("x-blockfield-provenance", "")
        if not provenance.startswith(f"cpv2/app.py@{contract.CONTRACT_SHA[:7]}"):
            failures.append(f"unprovenanced path (transcribe, never invent): {path}")
    schemas = doc.get("components", {}).get("schemas", {})
    for name in contract.MUTATION_SCHEMAS:
        schema = schemas.get(name)
        if schema is None:
            failures.append(f"missing schema: {name}")
            continue
        smuggled = set(schema.get("properties", {})) & set(
            contract.FORBIDDEN_BODY_FIELDS
        )
        if smuggled:
            failures.append(f"{name} smuggles authority fields: {sorted(smuggled)}")
        role = schema.get("properties", {}).get("role")
        if role is not None and (
            name != contract.ROLE_FIELD_OWNER or "owner" in role.get("enum", [])
        ):
            failures.append(f"{name} carries an authority role field")
        if schema.get("additionalProperties", True):
            failures.append(f"{name} must set additionalProperties: false (Strict)")
    extensions = info.get("x-blockfield", {})
    if sorted(extensions.get("action-kinds", [])) != sorted(contract.ACTION_KINDS):
        failures.append("x-blockfield.action-kinds drifted from core.py:428")
    if sorted(extensions.get("ack-states", [])) != sorted(contract.ACK_STATES):
        failures.append("x-blockfield.ack-states drifted from models.py")
    if sorted(extensions.get("setting-namespaces", [])) != sorted(
        contract.SETTING_NAMESPACES
    ):
        failures.append("x-blockfield.setting-namespaces drifted from settings.py")
    return failures


def main(argv):
    failures = []
    for name in argv:
        try:
            doc = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            failures.append(f"{name}: unreadable fixture ({error})")
            continue
        failures.extend(f"{name}: {reason}" for reason in check(doc))
    if failures:
        print("\n".join(f"FAIL {line}" for line in failures))
        return 1
    print(f"PASS {len(argv)} fixture(s) match CP v2 pin {contract.CONTRACT_SHA[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["scripts/dev/tests/fixtures/cp-v2-openapi.json"]))
