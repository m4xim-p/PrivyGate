"""OpenAPI drift test: keep process_api.yaml, FastAPI schema and models in sync."""

from __future__ import annotations

from pathlib import Path

import yaml

from app.main import app
from app.models import ProcessRequest, ProcessResponse

ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "process_api.yaml"


def _load_spec() -> dict:
    with SPEC_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_spec_file_exists_and_is_valid() -> None:
    spec = _load_spec()
    assert spec["openapi"] == "3.0.3"
    assert "/process" in spec["paths"]


def test_fastapi_exposes_process_endpoint() -> None:
    schema = app.openapi()
    assert "/process" in schema["paths"]
    post = schema["paths"]["/process"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessRequest"
    }
    assert post["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessResponse"
    }


def test_models_match_spec_schema() -> None:
    spec = _load_spec()
    schemas = spec["components"]["schemas"]

    req = schemas["ProcessRequest"]
    assert set(req["required"]) == {"payload", "payload_id"}
    assert req["properties"]["payload"]["type"] == "string"
    assert req["properties"]["payload_id"]["type"] == "string"

    resp = schemas["ProcessResponse"]
    assert resp["required"] == ["result"]
    assert resp["properties"]["result"]["type"] == "string"


def test_models_match_fastapi_schema() -> None:
    schema = app.openapi()
    schemas = schema["components"]["schemas"]

    req = schemas["ProcessRequest"]
    assert set(req["required"]) == {"payload", "payload_id"}
    assert req["properties"]["payload"]["type"] == "string"
    assert req["properties"]["payload_id"]["type"] == "string"

    resp = schemas["ProcessResponse"]
    assert resp["required"] == ["result"]
    assert resp["properties"]["result"]["type"] == "string"


def test_spec_and_fastapi_agree_on_request_fields() -> None:
    spec = _load_spec()["components"]["schemas"]["ProcessRequest"]
    fastapi = app.openapi()["components"]["schemas"]["ProcessRequest"]

    assert set(spec["required"]) == set(fastapi["required"])
    assert set(spec["properties"]) == set(fastapi["properties"])


def test_pydantic_models_are_consistent() -> None:
    # The pydantic models must expose exactly the fields the spec declares.
    req_fields = set(ProcessRequest.model_fields)
    resp_fields = set(ProcessResponse.model_fields)
    assert req_fields == {"payload", "payload_id"}
    assert resp_fields == {"result"}


def test_response_codes_are_covered_by_contract() -> None:
    """Every FastAPI response code must be covered by the contract spec.

    The contract declares 200, 429, 4XX, 5XX. FastAPI declares concrete codes
    (200, 409, 413, 422, 429, 410, 500). Each concrete code must be covered by
    the contract's 200/429/4XX/5XX buckets.
    """
    spec = _load_spec()
    contract_codes = set(spec["paths"]["/process"]["post"]["responses"].keys())

    fastapi_codes = set(
        app.openapi()["paths"]["/process"]["post"]["responses"].keys()
    )

    for code in fastapi_codes:
        if code == "200" or code == "429":
            assert code in contract_codes, f"{code} must be declared in contract"
        elif code.startswith("4"):
            assert "4XX" in contract_codes, f"{code} must be covered by 4XX"
        elif code.startswith("5"):
            assert "5XX" in contract_codes, f"{code} must be covered by 5XX"


def test_method_is_post() -> None:
    spec = _load_spec()
    fastapi = app.openapi()
    assert "post" in spec["paths"]["/process"]
    assert "post" in fastapi["paths"]["/process"]


def test_request_body_matches() -> None:
    spec = _load_spec()["paths"]["/process"]["post"]["requestBody"]
    fastapi = app.openapi()["paths"]["/process"]["post"]["requestBody"]

    assert spec["required"] is True
    assert fastapi["required"] is True
    assert spec["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessRequest"
    }
    assert fastapi["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessRequest"
    }


def test_success_response_schema_matches() -> None:
    spec = _load_spec()["paths"]["/process"]["post"]["responses"]["200"]
    fastapi = app.openapi()["paths"]["/process"]["post"]["responses"]["200"]

    assert spec["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessResponse"
    }
    assert fastapi["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProcessResponse"
    }


def test_schema_field_types_match() -> None:
    """Field types and required flags must match between contract and FastAPI."""
    spec = _load_spec()["components"]["schemas"]
    fastapi = app.openapi()["components"]["schemas"]

    for name in ("ProcessRequest", "ProcessResponse"):
        s = spec[name]
        f = fastapi[name]
        assert s["type"] == f["type"] == "object"
        assert set(s["required"]) == set(f["required"])
        assert set(s["properties"]) == set(f["properties"])
        for field in s["properties"]:
            assert s["properties"][field]["type"] == f["properties"][field]["type"]


def test_no_field_constraints_drift() -> None:
    """If the contract declares field constraints, FastAPI must match them."""
    spec = _load_spec()["components"]["schemas"]
    fastapi = app.openapi()["components"]["schemas"]

    for name in ("ProcessRequest", "ProcessResponse"):
        s = spec[name]
        f = fastapi[name]
        for field in s["properties"]:
            spec_constraints = {
                k: v for k, v in s["properties"][field].items()
                if k in {"minLength", "maxLength", "pattern", "minimum", "maximum"}
            }
            fastapi_constraints = {
                k: v for k, v in f["properties"][field].items()
                if k in {"minLength", "maxLength", "pattern", "minimum", "maximum"}
            }
            assert spec_constraints == fastapi_constraints, (
                f"{name}.{field} constraints drift: "
                f"contract={spec_constraints} fastapi={fastapi_constraints}"
            )
