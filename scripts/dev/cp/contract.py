"""Frozen CP v2 wire-contract pin (unit CP-S7b).

Transcribed from control-plane origin/main `6df1f95` (v2 core, PR #15);
`cf660e4` (#17) touches U1 only, so the v2 pin stays at `6df1f95`.
Consumers in steps 3-6 build against this record, never an invented API:
if the contract lacks something, STOP PARTIAL with the exact gap.
"""

CONTRACT_SHA = "6df1f954d43f128661898931bbed4b947a5cbff3"

# "METHOD path" with cpv2/app.py source lines at CONTRACT_SHA.
ROUTES = (
    "GET /health",  # 309
    "GET /v1/openapi.json",  # 313
    "GET /v2/me",  # 317
    "POST /v2/owner/challenges",  # 342, 201
    "GET /v2/settings/catalog",  # 358
    "GET /v2/settings/{namespace}/{target}",  # 380
    "PATCH /v2/settings/{namespace}/{target}",  # 401, 202
    "POST /v2/settings/{namespace}/{target}/validate",  # 419
    "POST /v2/settings/{namespace}/{target}/rollback",  # 427, 202
    "POST /v2/staff/grants",  # 446, 202
    "POST /v2/accounts/{account_id}/test-status",  # 454, 202
    "POST /v2/actions/{kind}",  # 469, 202
    "POST /v2/delegations",  # 475, 201
    "DELETE /v2/sessions/current",  # 479
    "GET /v2/operations/{operation_id}",  # 498
    "GET /v2/audit",  # 503
    "GET /v1/gate",  # 548, legacy read-only compatibility view
    "GET /v2/events",  # 567, user-scoped SSE
    "GET /v2/agents/{cid}/snapshot",  # 628
    "POST /v2/agents/{cid}/acks",  # 643
    "GET /v2/agents/{cid}/events",  # 647, private SSE
)

# Strict models (extra="forbid"): body actor/via are rejected everywhere —
# the server derives authority from the bearer (cpv2/models.py, settings.py).
# `role` is forbidden as authority attribution too, with one exception:
# GrantChange.role carries the *granted* lower-role value (owner excluded by
# the Literal), not the caller's authority (E3, §7.1).
FORBIDDEN_BODY_FIELDS = ("actor", "via")
ROLE_FIELD_OWNER = "GrantChange"

MUTATION_SCHEMAS = (
    "Mutation",
    "SettingsChange",
    "Rollback",
    "GrantChange",
    "TestStatus",
    "Action",
    "Delegation",
    "Ack",
    "Challenge",
)

# "METHOD path" routes that take a JSON body, bound to their schema component.
# The harness resolves each route's requestBody $ref and runs the authority
# checks on the referenced schema; an unbound mutating route is a failure.
ROUTE_BODY = {
    "POST /v2/owner/challenges": "Challenge",
    "PATCH /v2/settings/{namespace}/{target}": "SettingsChange",
    "POST /v2/settings/{namespace}/{target}/validate": "SettingsChange",
    "POST /v2/settings/{namespace}/{target}/rollback": "Rollback",
    "POST /v2/staff/grants": "GrantChange",
    "POST /v2/accounts/{account_id}/test-status": "TestStatus",
    "POST /v2/actions/{kind}": "Action",
    "POST /v2/delegations": "Delegation",
    "POST /v2/agents/{cid}/acks": "Ack",
}

# core.py:428 — the only typed action kinds the server accepts.
ACTION_KINDS = ("moderation.ban", "moderation.kick")

# models.py Ack.state; OperationReceipt.status adds accepted/expired/partial.
ACK_STATES = (
    "received",
    "validated",
    "applied",
    "effective",
    "rejected",
    "unknown",
)

# settings.py DEFINITIONS — the only v2 namespaces; more need reviewed code.
SETTING_NAMESPACES = ("gameplay", "gate")

# Markers that prove MCP reaches past the §7 API: U1 shared service tokens,
# the bootstrap secret, or a direct database driver. site-mcp must be a thin
# §7 client (design §7.1) holding only per-call bearers, never these.
FORBIDDEN_MCP_MARKERS = (
    "BLOCKFIELD_CP_GATE_WRITE_TOKEN",
    "BLOCKFIELD_CP_GATE_READ_TOKEN",
    "CP_BOOTSTRAP_ADMIN_SECRET",
    "CP_DATABASE_URL",
    "psycopg",
    "sqlite3",
)
