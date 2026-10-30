"""Classify model failures without retaining provider text or request metadata."""

import socket
import ssl

from openai import APIConnectionError, APIResponseValidationError, APITimeoutError
from pydantic import ValidationError

_PARAMETERS = frozenset(
    {
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "model",
        "base_url",
        "temperature",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "response_format",
        "stream",
    }
)
_TOOL_PARAMETERS = frozenset({"tools", "tool_choice", "parallel_tool_calls"})
_PROVIDER_CODES = frozenset(
    {
        "invalid_api_key",
        "insufficient_quota",
        "quota_exceeded",
        "insufficient_balance",
        "rate_limit_exceeded",
        "model_not_found",
        "model_not_supported",
        "unsupported_parameter",
        "unsupported_value",
        "invalid_parameter",
        "invalid_request_error",
        "permission_denied",
        "tool_use_failed",
        "function_calling_not_supported",
        "tool_choice_not_supported",
    }
)
_TOOL_CODES = frozenset(
    {"tool_use_failed", "function_calling_not_supported", "tool_choice_not_supported"}
)
_CONFIG_STAGES = frozenset(
    {"configuration", "config", "settings", "build", "model_build", "build_model", "migration"}
)
_NETWORK_NAMES = frozenset(
    {"ConnectError", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ProxyError"}
)


def _chain(exc):
    """Inspect only types, never serialize exception objects or their dictionaries."""
    seen = set()
    for _ in range(8):
        if exc is None or id(exc) in seen:
            return
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__


def _network_type(exc):
    chain = tuple(_chain(exc))
    if any(isinstance(item, socket.gaierror) for item in chain):
        return "dns"
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return "tls"
    if any(type(item).__name__ == "ProxyError" for item in chain):
        return "proxy"
    if any(
        isinstance(item, (APITimeoutError, TimeoutError))
        or type(item).__name__ in {"ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout"}
        for item in chain
    ):
        return "timeout"
    if any(isinstance(item, ConnectionRefusedError) for item in chain):
        return "connection_refused"
    return "connection"


def _is_network(exc):
    if isinstance(
        exc, (APIConnectionError, ConnectionError, TimeoutError, socket.gaierror, ssl.SSLError)
    ):
        return True
    cls = type(exc)
    return cls.__name__ in _NETWORK_NAMES and cls.__module__.startswith(("httpx", "httpcore"))


def _safe_parameter(exc):
    raw = getattr(exc, "param", None)
    if not isinstance(raw, str):
        return None
    # Only a known root is retained; arbitrary schema paths can contain reflected data.
    root = raw.strip().casefold().split(".", 1)[0].split("[", 1)[0]
    return root if root in _PARAMETERS else None


def _unsupported_tool_message(exc):
    # This text is inspected only to select a fixed label. Never return/store it.
    message = getattr(exc, "message", None)
    if not isinstance(message, str):
        return False
    message = message.casefold()
    unsupported = any(
        word in message
        for word in ("unsupported", "not support", "unrecognized", "unknown parameter")
    )
    tool = any(
        word in message
        for word in (
            "tool_choice",
            "parallel_tool_calls",
            "tools",
            "function calling",
            "tool calling",
        )
    )
    return unsupported and tool


def safe_model_error(exc: Exception, stage: str = "request") -> dict:
    """Return static diagnosis plus tightly allowlisted, non-secret metadata.

    Model API errors may echo credentials in their message, body, code, param,
    URL or headers. Arbitrary strings from those fields are never copied out.
    """
    result = {"error_type": type(exc).__name__}
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["http_status"] = status
    else:
        status = None
    parameter = _safe_parameter(exc)
    if parameter:
        result["parameter"] = parameter
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _PROVIDER_CODES:
        result["provider_code"] = code
    else:
        code = None

    if isinstance(exc, PermissionError):
        category, reason = "permission", "本地文件或进程访问被拒绝，请检查本地执行权限。"
    elif _is_network(exc):
        category = "network"
        subtype = _network_type(exc)
        result["network_subtype"] = subtype
        reason = {
            "dns": "模型服务域名解析失败，请检查 DNS 与服务地址。",
            "tls": "模型连接的 TLS 校验失败，请检查证书与代理配置。",
            "proxy": "模型请求的代理连接失败，请检查代理可达性。",
            "timeout": "模型请求超时，请检查网络及服务响应时间。",
            "connection_refused": "模型服务连接被拒绝，请检查地址及服务监听状态。",
            "connection": "无法连接模型服务，请检查网络、代理、DNS 与 TLS。",
        }[subtype]
    elif status == 401:
        category, reason = "authentication", "模型服务拒绝认证，请核对现有密钥及认证方式。"
    elif status == 403:
        category, reason = "permission", "模型服务拒绝访问，请核对账号、模型授权与访问策略。"
    elif status == 404:
        category = "configuration"
        reason = "服务端点或模型资源不可用，也可能未向当前账号开放；不能据此判断密钥错误。"
    elif status == 407:
        category, reason = "network", "网络代理要求认证，请检查本地代理配置。"
        result["network_subtype"] = "proxy"
    elif status in {402, 429}:
        category, reason = (
            "rate_limit_or_quota",
            "模型服务返回限流或额度限制，请检查账户配额及调用频率。",
        )
    elif status in {400, 422}:
        if (
            parameter in _TOOL_PARAMETERS
            or code in _TOOL_CODES
            or (parameter is None and _unsupported_tool_message(exc))
        ):
            category = "tool_calling_compatibility"
            reason = "工具调用参数或 schema 未被服务接受，请核实模型与接口的工具调用约定。"
        else:
            category, reason = "configuration", "请求参数未被服务接受，请检查模型、端点和参数配置。"
    elif isinstance(exc, APIResponseValidationError) or stage == "response":
        category = "tool_calling_compatibility"
        reason = "模型响应不符合当前工具调用约定，请核实响应结构与有效工具调用。"
    elif status is not None and status >= 500:
        category, reason = "model_service", "模型服务或上游网关返回服务端错误，请检查服务状态。"
    elif (
        isinstance(exc, (ValidationError, ValueError, TypeError, FileNotFoundError))
        or stage in _CONFIG_STAGES
    ):
        category, reason = "configuration", "模型配置无法加载或客户端参数无效，请检查本地配置。"
    else:
        category, reason = "model_service", "模型请求未完成，需进一步检查服务端及客户端兼容性。"
    return {"category": category, "reason": reason, **result}
