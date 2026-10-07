"""受限接口视图：从折叠状态按角色生成当前责任视图并校验。

接口必须能说明：
1. 当前责任人（承接门店，及其依据事件）；
2. 采用的服务计划版本（及其依据事件）；
3. 每条仍有效提醒的回访窗口与事实依据；
4. 系统不会自动作出诊断。

普通店员（clerk）走最小知情：看不到计划细节、事实依据内容与病史，
只能看到继续跟进所需的门店、窗口与义务标识。
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ContractIssue, parse_datetime
from .stream import StreamState

DIAGNOSIS_DISCLAIMER = "本系统不自动作出诊断；健康判断由具备资质的药师作出。"

_PHARMACIST_SECTIONS = ["responsible", "plan", "reminders", "open_obligations", "medical_history"]
_CLERK_SECTIONS = ["responsible", "reminders", "open_obligations"]


def build_responsibility_view(
    state: StreamState,
    subject_id: str,
    *,
    as_of: str,
    viewer_role: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """根据保存状态构造受限接口视图（纯函数，不修改状态）。"""
    plan = state.plans.get(plan_id) if plan_id else _latest_plan(state, subject_id)

    responsible_store = state.subject_responsible.get(subject_id)
    if responsible_store is None and plan is not None:
        responsible_store = plan["store_id"]

    reminders = _active_reminders(state, subject_id, as_of)
    obligations = [
        {
            "obligation_id": ob.obligation_id,
            "owner_store": ob.owner_store,
            "status": ob.status,
            "escalated": ob.escalated_event_id is not None,
        }
        for ob in state.obligations.values()
        if ob.subject_id == subject_id
    ]

    responsible: dict[str, Any] = {
        "store_id": responsible_store or "",
        "basis_event_id": _responsible_basis(state, subject_id, responsible_store, plan),
    }
    if viewer_role != "clerk" and plan is not None:
        responsible["pharmacist_id"] = plan["pharmacist_id"]

    plan_block = {
        "plan_id": plan["plan_id"] if plan is not None else (plan_id or ""),
        "plan_version": plan["version"] if plan is not None else 0,
        "basis_event_id": plan.get("basis_event_id", plan.get("activated_event_id", "")) if plan is not None else "",
    }

    full_view: dict[str, Any] = {
        "subject_id": subject_id,
        "as_of": as_of,
        "viewer_role": viewer_role,
        "responsible": responsible,
        "plan": plan_block,
        "active_reminders": reminders,
        "open_obligations": sorted(obligations, key=lambda item: item["obligation_id"]),
        "diagnosis_disclaimer": DIAGNOSIS_DISCLAIMER,
        "visible_sections": list(_PHARMACIST_SECTIONS if viewer_role != "clerk" else _CLERK_SECTIONS),
    }
    return _project_for_role(full_view, viewer_role)


def _latest_plan(state: StreamState, subject_id: str) -> dict[str, Any] | None:
    candidates = [plan for plan in state.plans.values() if plan["subject_id"] == subject_id]
    return max(candidates, key=lambda plan: plan["version"], default=None)


def _active_reminders(state: StreamState, subject_id: str, as_of: str) -> list[dict[str, Any]]:
    moment = parse_datetime(as_of)
    result: list[dict[str, Any]] = []
    for reminder_id, reminder in state.reminders.items():
        if reminder["subject_id"] != subject_id:
            continue
        obligation = state.obligations.get(reminder["obligation_id"])
        if obligation is not None and obligation.status == "fulfilled":
            continue
        window_end = parse_datetime(reminder["window_end"])
        if moment is not None and window_end is not None and moment > window_end:
            continue  # 窗口已过且未被义务升级承接的提醒不再展示
        result.append(
            {
                "reminder_id": reminder_id,
                "window_start": reminder["window_start"],
                "window_end": reminder["window_end"],
                "factual_basis": reminder["factual_basis"],
                "basis_event_id": reminder["fired_event_id"],
            }
        )
    return sorted(result, key=lambda item: item["window_start"])


def _responsible_basis(
    state: StreamState,
    subject_id: str,
    responsible_store: str | None,
    plan: dict[str, Any] | None,
) -> str:
    if responsible_store is not None and responsible_store != (plan["store_id"] if plan else None):
        for handoff in state.handoffs.values():
            proposal = handoff.get("proposal", {})
            if proposal.get("subject_id") == subject_id and proposal.get("to_store") == responsible_store:
                return handoff["completed_event_id"]
    if plan is not None:
        return plan.get("basis_event_id", plan.get("activated_event_id", ""))
    return ""


def _project_for_role(view: dict[str, Any], role: str) -> dict[str, Any]:
    """按角色裁剪字段；普通店员无权翻看计划细节、提醒事实依据与病史。"""
    if role != "clerk":
        return view
    clerk_view = {
        "subject_id": view["subject_id"],
        "as_of": view["as_of"],
        "viewer_role": "clerk",
        "responsible": {"store_id": view["responsible"]["store_id"], "basis_event_id": view["responsible"]["basis_event_id"]},
        "plan": {"plan_id": view["plan"]["plan_id"], "plan_version": view["plan"]["plan_version"], "basis_event_id": view["plan"]["basis_event_id"]},
        "active_reminders": [
            {
                "reminder_id": item["reminder_id"],
                "window_start": item["window_start"],
                "window_end": item["window_end"],
                "basis_event_id": item["basis_event_id"],
            }
            for item in view["active_reminders"]
        ],
        "open_obligations": view["open_obligations"],
        "diagnosis_disclaimer": view["diagnosis_disclaimer"],
        "visible_sections": view["visible_sections"],
    }
    return clerk_view


def validate_view(view: Any, schema: Mapping[str, Any]) -> list[ContractIssue]:
    """校验受限接口视图结构、角色可见性与强制声明。"""
    if not isinstance(view, Mapping):
        return [ContractIssue("$", "object_required", "视图必须是 JSON 对象")]

    issues: list[ContractIssue] = []
    for field in schema.get("required", []):
        if field not in view:
            issues.append(ContractIssue(field, "required", "视图缺少必填区块"))

    role = view.get("viewer_role")
    allowed_roles = schema["properties"]["viewer_role"]["enum"]
    if isinstance(role, str) and role not in allowed_roles:
        issues.append(ContractIssue("viewer_role", "unsupported_value", "访问角色未登记"))

    as_of = view.get("as_of")
    if isinstance(as_of, str):
        moment = parse_datetime(as_of)
        if moment is None or moment.tzinfo is None:
            issues.append(ContractIssue("as_of", "timezone_required", "视图时点必须包含时区"))

    if view.get("diagnosis_disclaimer") != DIAGNOSIS_DISCLAIMER:
        issues.append(ContractIssue("diagnosis_disclaimer", "disclaimer_required", "受限接口必须声明系统不自动作出诊断"))

    responsible = view.get("responsible")
    if not isinstance(responsible, Mapping) or not responsible.get("store_id"):
        issues.append(ContractIssue("responsible.store_id", "required", "视图必须说明当前责任门店"))
    if not isinstance(responsible, Mapping) or not responsible.get("basis_event_id"):
        issues.append(ContractIssue("responsible.basis_event_id", "required", "当前责任人必须给出依据事件"))

    plan = view.get("plan")
    if not isinstance(plan, Mapping) or not plan.get("plan_id") or not plan.get("plan_version"):
        issues.append(ContractIssue("plan", "required", "视图必须说明采用的计划版本"))
    if isinstance(plan, Mapping) and not plan.get("basis_event_id"):
        issues.append(ContractIssue("plan.basis_event_id", "required", "计划版本必须给出依据事件"))

    reminders = view.get("active_reminders")
    if isinstance(reminders, list):
        for index, reminder in enumerate(reminders):
            prefix = f"active_reminders[{index}]"
            if not isinstance(reminder, Mapping) or not reminder.get("basis_event_id"):
                issues.append(ContractIssue(prefix, "basis_required", "每条提醒必须能追溯到事实事件"))
            if role != "clerk" and isinstance(reminder, Mapping) and not reminder.get("factual_basis"):
                issues.append(ContractIssue(f"{prefix}.factual_basis", "basis_required", "提醒必须说明事实依据"))

    sections = view.get("visible_sections")
    if isinstance(sections, list) and role == "clerk" and "medical_history" in sections:
        issues.append(ContractIssue("visible_sections", "minimum_necessary", "普通店员无权翻看无关病史"))

    return sorted(issues, key=lambda issue: (issue.field, issue.code))
