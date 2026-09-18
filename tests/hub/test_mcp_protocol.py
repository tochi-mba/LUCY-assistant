"""MCP protocol checks that must not depend on FastAPI."""

from __future__ import annotations

import base64

import pytest

from lucy_api.core.config import Settings
from lucy_api.core.errors import LucyError
from lucy_api.mcp.catalog import WEFTAI_DESCRIBE_OPERATIONS, WEFTAI_GET_RESULT, listed
from lucy_api.mcp.headers import (
    assert_method_header,
    assert_name_header,
    decode_mirrored,
    lookup,
    protocol_version,
)
from lucy_api.mcp.origin import allowed_origins, assert_origin
from lucy_api.mcp.protocol import (
    CURRENT,
    HEADER_MISMATCH_DETAIL,
    INSTRUCTIONS,
    LEGACY,
    META_VERSION,
    ORIGIN_REFUSED,
    RpcError,
    error_body,
    header_mismatch,
    parse_error,
    result_body,
    unsupported_version,
)


def test_a_missing_origin_is_allowed() -> None:
    assert_origin(None, Settings(_env_file=None))
    assert_origin("  ", Settings(_env_file=None))


def test_the_hubs_own_origin_is_allowed() -> None:
    settings = Settings(_env_file=None, host="127.0.0.1", port=8000)
    assert "http://127.0.0.1:8000" in allowed_origins(settings)
    assert "http://localhost:8000" in allowed_origins(settings)
    assert_origin("http://127.0.0.1:8000", settings)
    assert_origin("http://localhost:8000/", settings)


def test_a_foreign_origin_is_one_refusal_that_does_not_echo_the_host() -> None:
    with pytest.raises(LucyError) as caught:
        assert_origin("https://evil.example", Settings(_env_file=None))
    assert caught.value.status == 403
    assert caught.value.code == "origin-refused"
    assert str(caught.value) == ORIGIN_REFUSED
    assert "evil" not in str(caught.value)


def test_an_unparseable_origin_is_the_same_refusal() -> None:
    with pytest.raises(LucyError) as caught:
        assert_origin("not-a-url", Settings(_env_file=None))
    assert caught.value.status == 403


def test_http_origins_without_a_port_normalise_to_80() -> None:
    with pytest.raises(LucyError):
        assert_origin("http://evil.example", Settings(_env_file=None))


def test_a_non_loopback_host_does_not_alias_localhost() -> None:
    settings = Settings(_env_file=None, host="lucy.example", port=8000)
    allowed = allowed_origins(settings)
    assert "http://lucy.example:8000" in allowed
    assert "http://localhost:8000" not in allowed


def test_modern_requests_require_matching_version_headers() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {"_meta": {META_VERSION: CURRENT}},
    }
    assert protocol_version({"MCP-Protocol-Version": CURRENT}, payload) == CURRENT


def test_a_legacy_tools_list_does_not_need_meta() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert protocol_version({"mcp-protocol-version": LEGACY}, payload) == LEGACY


def test_initialize_selects_legacy_even_without_headers() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert protocol_version({}, payload) == LEGACY


def test_an_unknown_version_lists_what_is_supported() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": {META_VERSION: "1900-01-01"}},
    }
    with pytest.raises(RpcError) as caught:
        protocol_version({"MCP-Protocol-Version": "1900-01-01"}, payload)
    assert caught.value.code == unsupported_version("1900-01-01").code
    assert caught.value.data == {"supported": [CURRENT, LEGACY], "requested": "1900-01-01"}


def test_a_modern_header_must_match_meta() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {"_meta": {META_VERSION: LEGACY}},
    }
    with pytest.raises(RpcError) as mismatch:
        protocol_version({"MCP-Protocol-Version": CURRENT}, payload)
    assert mismatch.value.message == HEADER_MISMATCH_DETAIL
    assert LEGACY not in mismatch.value.message


def test_base64_name_headers_decode_before_comparison() -> None:
    encoded = base64.b64encode(b"lucy_chat").decode("ascii")
    assert decode_mirrored(f"=?base64?{encoded}?=") == "lucy_chat"
    with pytest.raises(RpcError):
        decode_mirrored("=?base64?@@@?=")
    invalid_utf8 = base64.b64encode(b"\xff").decode("ascii")
    with pytest.raises(RpcError):
        decode_mirrored(f"=?base64?{invalid_utf8}?=")


def test_lookup_is_case_insensitive() -> None:
    assert lookup({"MCP-Protocol-Version": CURRENT}, "mcp-protocol-version") == CURRENT


def test_parse_errors_carry_a_null_id() -> None:
    body = error_body(None, parse_error(), include_id=True)
    assert body["id"] is None
    assert body["error"]["code"] == parse_error().code


def test_successful_results_always_declare_complete() -> None:
    body = result_body(7, {"ok": True})
    assert body["id"] == 7
    assert body["result"]["resultType"] == "complete"
    assert body["result"]["ok"] is True


def test_weftai_tool_descriptions_match_the_library() -> None:
    import inspect

    import weftai.mcp as weftai_mcp

    source = inspect.getsource(weftai_mcp.create_mcp_server)
    assert WEFTAI_DESCRIBE_OPERATIONS in source
    # weftai splits this across two literals; the catalogue stores the concatenated value.
    first, _, rest = WEFTAI_GET_RESULT.partition(" full set")
    assert first + " " in source
    assert "full set" + rest in source


def test_the_listed_surface_is_sorted_and_annotated() -> None:
    names = [tool["name"] for tool in listed("Run one or more steps.")]
    assert names == sorted(names)
    run_plan = next(tool for tool in listed("Run one or more steps.") if tool["name"] == "run_plan")
    assert run_plan["description"] == "Run one or more steps."
    assert run_plan["annotations"]["openWorldHint"] is True
    capabilities = next(tool for tool in listed("x") if tool["name"] == "lucy_list_capabilities")
    assert capabilities["annotations"]["readOnlyHint"] is True
    assert "music" in INSTRUCTIONS
    assert "spotify" not in INSTRUCTIONS.lower()


def test_header_mismatch_error_is_the_spec_code() -> None:
    error = header_mismatch()
    assert error.code == -32020
    assert error.status == 400


def test_a_plain_name_header_is_compared_as_is() -> None:
    assert decode_mirrored("lucy_chat") == "lucy_chat"


def test_legacy_requests_skip_the_method_header() -> None:
    assert_method_header({}, "tools/list", LEGACY)


def test_a_modern_method_header_must_match() -> None:
    with pytest.raises(RpcError) as caught:
        assert_method_header({"Mcp-Method": "tools/list"}, "server/discover", CURRENT)
    assert caught.value.message == HEADER_MISMATCH_DETAIL


def test_name_headers_are_only_required_on_tools_call() -> None:
    assert_name_header({}, {"method": "tools/list"}, CURRENT)


def test_a_matching_name_header_is_accepted() -> None:
    assert_name_header(
        {"Mcp-Name": "lucy_chat"},
        {"method": "tools/call", "params": {"name": "lucy_chat"}},
        CURRENT,
    )


def test_a_tools_call_without_a_name_is_a_header_mismatch() -> None:
    with pytest.raises(RpcError):
        assert_name_header({}, {"method": "tools/call", "params": {}}, CURRENT)


def test_missing_modern_meta_is_invalid_params() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": []}
    with pytest.raises(RpcError) as caught:
        protocol_version({"MCP-Protocol-Version": CURRENT}, payload)
    from lucy_api.mcp.protocol import INVALID_PARAMS, MISSING_META

    assert caught.value.code == INVALID_PARAMS
    assert caught.value.message == MISSING_META


def test_an_empty_declared_version_is_missing_meta() -> None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "server/discover",
        "params": {"_meta": {META_VERSION: ""}},
    }
    with pytest.raises(RpcError):
        protocol_version({"MCP-Protocol-Version": CURRENT}, payload)


def test_a_missing_protocol_header_is_a_mismatch() -> None:
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    with pytest.raises(RpcError):
        protocol_version({}, payload)


def test_error_bodies_omit_id_when_asked() -> None:
    body = error_body(1, parse_error(), include_id=False)
    assert "id" not in body
