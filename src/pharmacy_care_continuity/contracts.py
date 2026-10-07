"""领域事件交换契约校验。

只做单事件、与业务状态无关的结构性校验：信封字段、枚举、时间时区、
版本号以及各事件类型的必需载荷。跨事件的业务规则见 ``stream`` 模块，
受限接口视图见 ``views`` 模块。校验器不修改调用方输入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class ContractIssue:
    field: str
    code: str
    message: str


def timezone_is_explicit(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _validate_envelope(payload: Any, schema: Mapping[str, Any]) -> list[ContractIssue]:
    if not isinstance(payload, Mapping):
        return [ContractIssue("$", "object_required", "事件必须是 JSON 对象")]

    issues: list[ContractIssue] = []
    for field in schema.get("required", []):
        if field not in payload:
            issues.append(ContractIssue(str(field), "required", "缺少必填字段"))

    for field in ("event_id", "event_type", "aggregate_type", "aggregate_id"):
        if field in payload and (not isinstance(payload[field], str) or not payload[field].strip()):
            issues.append(ContractIssue(field, "non_empty_string", "字段必须是非空字符串"))

    version = payload.get("version")
    if "version" in payload and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        issues.append(ContractIssue("version", "positive_integer", "版本必须是正整数"))

    occurred_at = payload.get("occurred_at")
    if "occurred_at" in payload and (not isinstance(occurred_at, str) or not timezone_is_explicit(occurred_at)):
        issues.append(ContractIssue("occurred_at", "timezone_required", "发生时间必须包含时区"))

    properties = schema.get("properties", {})
    for field in ("event_type", "aggregate_type"):
        allowed = properties.get(field, {}).get("enum", [])
        value = payload.get(field)
        if isinstance(value, str) and allowed and value not in allowed:
            issues.append(ContractIssue(field, "unsupported_value", "字段值未在契约中登记"))

    return issues


def _validate_payload_shape(event: Mapping[str, Any], schema: Mapping[str, Any]) -> list[ContractIssue]:
    event_type = event.get("event_type")
    event_payload = event.get("payload")
    if not isinstance(event_type, str) or not isinstance(event_payload, Mapping):
        if "payload" in event and not isinstance(event_payload, Mapping):
            return [ContractIssue("payload", "object_required", "事件载荷必须是 JSON 对象")]
        return []

    issues: list[ContractIssue] = []
    required = schema.get("payload_required_by_event", {}).get(event_type, [])
    for field in required:
        if field not in event_payload:
            issues.append(ContractIssue(f"payload.{field}", "required", "事件载荷缺少必填字段"))

    enums_table = schema.get("enums", {})
    for field, enum_name in schema.get("payload_enums", {}).items():
        if field not in event_payload:
            continue
        value = event_payload[field]
        allowed = enums_table.get(enum_name, [])
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if isinstance(item, str) and allowed and item not in allowed:
                issues.append(ContractIssue(f"payload.{field}", "unsupported_value", "载荷字段值未在契约中登记"))

    for field in schema.get("payload_datetime_fields", []):
        value = event_payload.get(field)
        if isinstance(value, str) and not timezone_is_explicit(value):
            issues.append(ContractIssue(f"payload.{field}", "timezone_required", "载荷时间必须包含时区"))

    for field in schema.get("payload_positive_integer_fields", []):
        value = event_payload.get(field)
        if field in event_payload and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
            issues.append(ContractIssue(f"payload.{field}", "positive_integer", "载荷字段必须是正整数"))

    for field in schema.get("payload_string_array_fields", []):
        value = event_payload.get(field)
        if field in event_payload and (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) or not item.strip() for item in value)
        ):
            issues.append(ContractIssue(f"payload.{field}", "non_empty_string_array", "载荷字段必须是非空字符串数组"))

    for field in schema.get("payload_boolean_fields", []):
        if field in event_payload and not isinstance(event_payload[field], bool):
            issues.append(ContractIssue(f"payload.{field}", "boolean_required", "载荷字段必须是布尔值"))

    if event_type in ("PLAN_ACTIVATED", "PLAN_REVISED"):
        for forbidden in ("promotion_id", "discount", "coupon", "sales_offer"):
            if forbidden in event_payload:
                issues.append(
                    ContractIssue(
                        f"payload.{forbidden}",
                        "sales_influence_forbidden",
                        "销售优惠不得进入或改变健康服务计划",
                    )
                )

    return issues


def validate_event(payload: Any, schema: Mapping[str, Any]) -> list[ContractIssue]:
    """返回稳定排序的问题列表，且不修改输入。"""
    issues = _validate_envelope(payload, schema)
    if isinstance(payload, Mapping):
        issues.extend(_validate_payload_shape(payload, schema))
    return sorted(issues, key=lambda issue: (issue.field, issue.code))
