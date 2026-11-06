"""Verify-harness CLI: check an OpenAPI fixture against the frozen pin.

    python scripts/dev/cp/verify.py scripts/dev/tests/fixtures/cp-v2-openapi.json

Exit 0 with one PASS line per check group; otherwise the failing reasons.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cp import contract  # noqa: E402

OPENAPI_METHODS = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)

PATH_ITEM_KEYS = set(OPENAPI_METHODS) | {
    "parameters",
    "summary",
    "description",
    "servers",
    "$ref",
}


def _operation(paths, method, path):
    """Find the operation for METHOD path, matching the method case-insensitively."""
    item = paths.get(path, {})
    for key, value in item.items():
        if key.lower() == method.lower() and key.lower() in OPENAPI_METHODS:
            return value if isinstance(value, dict) else None
    return None


def _body_ref(operation):
    """Resolve a requestBody $ref; None when missing, inline or unresolvable."""
    if not isinstance(operation, dict):
        return None
    try:
        ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    except (KeyError, TypeError):
        return None
    return ref if isinstance(ref, str) else None


def _schema_failures(name, schema):
    """Authority checks on one schema; fail closed on every gap."""
    if schema is None:
        return [f"missing schema: {name}"]
    failures = []
    smuggled = set(schema.get("properties", {})) & set(contract.FORBIDDEN_BODY_FIELDS)
    if smuggled:
        failures.append(f"{name} smuggles authority fields: {sorted(smuggled)}")
    # Strict: only an explicit `false` passes; {} / missing / true all fail.
    if schema.get("additionalProperties") is not False:
        failures.append(f"{name} must set additionalProperties: false (Strict)")
    role = schema.get("properties", {}).get("role")
    if name == contract.ROLE_FIELD_OWNER:
        # A missing or renamed `role` is a failure, not a skip.
        enum = role.get("enum") if isinstance(role, dict) else None
        if not isinstance(enum, list) or "owner" in enum:
            failures.append(f"{name} must pin role to an explicit enum without owner")
    elif role is not None:
        failures.append(f"{name} carries an authority role field")
    return failures


def check_mcp(root):
    """Scan root recursively for §7-bypass markers; return (offenders, scanned)."""
    offenders, scanned = [], 0
    for path in sorted(Path(root).rglob("*.py")):
        scanned += 1
        text = path.read_text(encoding="utf-8")
        for marker in contract.FORBIDDEN_MCP_MARKERS:
            if marker in text:
                offenders.append(f"{path.name}: {marker}")
    return offenders, scanned


def check(doc):
    """Return a list of failure reasons; empty means the fixture is green."""
    failures = []
    info = doc.get("info", {})
    if info.get("x-blockfield-contract-sha") != contract.CONTRACT_SHA:
        failures.append("info.x-blockfield-contract-sha != frozen pin")
    paths = doc.get("paths", {})
    found = set()
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for key in item:
            low = key.lower()
            if low in OPENAPI_METHODS:
                found.add(f"{low.upper()} {path}")
            elif key not in PATH_ITEM_KEYS and not low.startswith("x-"):
                failures.append(f"unknown path-item key: {path} {key}")
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
        failures.extend(_schema_failures(name, schemas.get(name)))
    for route, name in contract.ROUTE_BODY.items():
        method, _, path = route.partition(" ")
        ref = _body_ref(_operation(paths, method, path))
        if ref != f"#/components/schemas/{name}":
            failures.append(f"{route} must bind requestBody to {name} via $ref")
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
