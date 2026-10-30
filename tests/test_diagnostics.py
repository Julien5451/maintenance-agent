"""Synthetic failures exercise redaction; these tests never contact a provider."""

import json
import socket
import ssl

import httpx2
import pytest
from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from pydantic import BaseModel, ValidationError

from maintenance_agent.diagnostics import safe_model_error

SECRET = "sk-fake-private-key-do-not-log-123456"
REQUEST = httpx2.Request(
    "POST",
    f"https://example.invalid/v1?api_key={SECRET}",
    headers={"Authorization": f"Bearer {SECRET}"},
)


def api_error(cls, status, *, code=SECRET, param=SECRET, message=SECRET):
    response = httpx2.Response(status, request=REQUEST, headers={"x-request-id": SECRET})
    return cls(
        message,
        response=response,
        body={"message": SECRET, "code": code, "param": param, "type": SECRET},
    )


def assert_redacted(result):
    text = json.dumps(result, ensure_ascii=False)
    assert SECRET not in text
    assert "example.invalid" not in text
    assert "Authorization" not in text
    assert "Bearer" not in text
    assert "body" not in result and "request" not in result and "response" not in result


@pytest.mark.parametrize(
    ("cls", "status", "category"),
    [
        (AuthenticationError, 401, "authentication"),
        (PermissionDeniedError, 403, "permission"),
        (NotFoundError, 404, "configuration"),
        (BadRequestError, 400, "configuration"),
        (UnprocessableEntityError, 422, "configuration"),
        (RateLimitError, 429, "rate_limit_or_quota"),
        (InternalServerError, 503, "model_service"),
    ],
)
def test_api_status_classification_never_echoes_credentials(cls, status, category):
    result = safe_model_error(api_error(cls, status))
    assert result["category"] == category
    assert result["http_status"] == status
    assert "provider_code" not in result and "parameter" not in result
    assert_redacted(result)


def test_404_does_not_assert_key_is_invalid():
    result = safe_model_error(api_error(NotFoundError, 404, code="model_not_found"))
    assert "不能据此判断密钥错误" in result["reason"]
    assert result["provider_code"] == "model_not_found"
    assert_redacted(result)


@pytest.mark.parametrize(
    "parameter", ["tools[0].function.parameters", "tool_choice", "parallel_tool_calls"]
)
def test_known_tool_parameter_is_normalized_and_classified(parameter):
    exc = api_error(
        BadRequestError, 400, param=parameter + "." + SECRET, code="unsupported_parameter"
    )
    result = safe_model_error(exc)
    assert result["category"] == "tool_calling_compatibility"
    assert result["parameter"] in {"tools", "tool_choice", "parallel_tool_calls"}
    assert result["provider_code"] == "unsupported_parameter"
    assert_redacted(result)


def test_unsupported_tool_message_selects_only_a_static_reason():
    exc = api_error(
        UnprocessableEntityError, 422, message="This model does not support tool_choice. " + SECRET
    )
    result = safe_model_error(exc)
    assert result["category"] == "tool_calling_compatibility"
    assert_redacted(result)


def test_known_non_tool_parameter_is_not_misclassified_from_incidental_tool_text():
    result = safe_model_error(
        api_error(
            BadRequestError,
            400,
            param="temperature",
            message="Unsupported temperature for tool calling " + SECRET,
        )
    )
    assert result["category"] == "configuration"
    assert result["parameter"] == "temperature"
    assert_redacted(result)


@pytest.mark.parametrize(
    ("cause", "subtype"),
    [
        (socket.gaierror(socket.EAI_NONAME, SECRET), "dns"),
        (ssl.SSLCertVerificationError(1, SECRET), "tls"),
        (httpx2.ProxyError(SECRET), "proxy"),
        (ConnectionRefusedError(SECRET), "connection_refused"),
    ],
)
def test_openai_connection_error_cause_is_safe_and_specific(cause, subtype):
    exc = APIConnectionError(message=SECRET, request=REQUEST)
    exc.__cause__ = cause
    result = safe_model_error(exc)
    assert result["category"] == "network"
    assert result["network_subtype"] == subtype
    assert "http_status" not in result
    assert_redacted(result)


def test_timeout_is_network_without_guessing_authentication_or_tool_support():
    result = safe_model_error(APITimeoutError(REQUEST))
    assert result["category"] == "network"
    assert result["network_subtype"] == "timeout"
    assert_redacted(result)


def test_local_permissions_are_distinct_from_remote_authentication():
    result = safe_model_error(PermissionError(SECRET), stage="configuration")
    assert result["category"] == "permission"
    assert "本地" in result["reason"]
    assert_redacted(result)


def test_invalid_settings_do_not_echo_pydantic_input_values():
    class ProbeSettings(BaseModel):
        timeout: int

    with pytest.raises(ValidationError) as captured:
        ProbeSettings(timeout=SECRET)
    result = safe_model_error(captured.value, stage="configuration")
    assert result["category"] == "configuration"
    assert_redacted(result)


def test_invalid_response_uses_compatibility_category_without_response_body():
    response = httpx2.Response(200, request=REQUEST)
    exc = APIResponseValidationError(response, {"echoed_secret": SECRET}, message=SECRET)
    result = safe_model_error(exc, stage="response")
    assert result["category"] == "tool_calling_compatibility"
    assert result["http_status"] == 200
    assert_redacted(result)


def test_proxy_authentication_and_quota_have_distinct_categories():
    proxy = safe_model_error(api_error(APIStatusError, 407))
    quota = safe_model_error(api_error(APIStatusError, 402, code="insufficient_quota"))
    assert proxy["category"] == "network" and proxy["network_subtype"] == "proxy"
    assert quota["category"] == "rate_limit_or_quota"
    assert_redacted(proxy)
    assert_redacted(quota)


def test_unknown_errors_do_not_echo_their_message():
    result = safe_model_error(RuntimeError(SECRET))
    assert result["category"] == "model_service"
    assert_redacted(result)


def test_build_model_stage_uses_configuration_for_unknown_local_failure():
    result = safe_model_error(RuntimeError(SECRET), stage="build_model")
    assert result["category"] == "configuration"
    assert_redacted(result)
