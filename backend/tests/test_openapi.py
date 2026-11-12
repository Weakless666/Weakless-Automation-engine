"""The OpenAPI document describes every response, success and error."""
from __future__ import annotations

from app.main import create_app

SPEC = create_app().openapi()
OPERATIONS = [(path, method, op) for path, item in SPEC["paths"].items() for method, op in item.items()]
PROBES = {"/api/v1/health", "/api/v1/health/ready"}  # unauthenticated liveness/readiness checks


def _schema(op: dict, code: str) -> dict:
    return op["responses"][code]["content"]["application/json"]["schema"]


def test_every_operation_documents_a_typed_success_response():
    problems = []
    for path, method, op in OPERATIONS:
        success = [c for c in op["responses"] if c.startswith("2")]
        if not success:
            problems.append(f"{method.upper()} {path}: no success response")
        elif "$ref" not in _schema(op, success[0]):
            problems.append(f"{method.upper()} {path}: untyped success response")
    assert len(OPERATIONS) > 150
    assert not problems, "\n".join(problems)


def test_every_operation_documents_the_error_envelope():
    problems = []
    for path, method, op in OPERATIONS:
        if path in PROBES:
            continue
        for code in ("401", "403", "404", "422", "429", "500"):
            if code not in op["responses"]:
                problems.append(f"{method.upper()} {path}: HTTP {code} not documented")
            elif not _schema(op, code)["$ref"].endswith("/ErrorEnvelope"):
                problems.append(f"{method.upper()} {path}: HTTP {code} is not the error envelope")
    assert not problems, "\n".join(problems)


def test_schemas_are_complete_and_resolvable():
    components = SPEC["components"]["schemas"]
    refs: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                refs.add(node["$ref"].split("/")[-1])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(SPEC)
    assert refs <= set(components), f"unresolved: {sorted(refs - set(components))}"
    assert "HTTPValidationError" not in refs  # the framework's default is replaced by our envelope everywhere
    assert len([n for n in components if n.endswith("Out")]) >= 40
