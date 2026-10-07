"""事件流业务规则：以纯折叠方式重放责任链事件。

规则对应运营约定：
- 同一聚合版本从 1 严格递增，同一顾客事件时间不倒流；
- 分项授权是提醒/回访/跨店交接/应急供应的闸门，撤回营销用途不影响法定证明；
- 药师只在资质窗口内签认亲自完成的服务，矛盾事实由未参与销售的药师核验；
- 同一事实键归并为一次服务事实；
- 只有尚未履行的义务随交接迁移，竞争承接中仅一家成功；
- 退出决定冻结未来服务，但保留法定购药与服务证明。

折叠不修改输入事件，状态可快照为 JSON，进程在交接中断后可从快照继续。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping

from .contracts import ContractIssue, parse_datetime


def _issue(index: int, event_id: str, path: str, code: str, message: str) -> ContractIssue:
    return ContractIssue(f"events[{index}].{path}", code, f"事件 {event_id}: {message}")


@dataclass
class ConsentRecord:
    subject_id: str
    grant_event_id: str
    store_id: str
    scopes: set[str]
    granted_at: str
    withdrawn: dict[str, str] = field(default_factory=dict)  # scope -> withdrawn event id


@dataclass
class QualificationRecord:
    pharmacist_id: str
    store_id: str
    valid_from: str
    valid_until: str
    revoked_event_id: str | None = None


@dataclass
class ObligationRecord:
    obligation_id: str
    subject_id: str
    owner_store: str
    opened_event_id: str
    status: str = "open"  # open / fulfilled
    escalated_event_id: str | None = None
    stock_event_id: str | None = None


@dataclass
class FactRecord:
    fact_key: str
    subject_id: str
    recordings: list[dict[str, Any]] = field(default_factory=list)
    conflicting: bool = False
    verification: dict[str, Any] | None = None


@dataclass
class StreamState:
    next_version: dict[str, int] = field(default_factory=dict)
    seen_event_ids: dict[str, int] = field(default_factory=dict)
    last_subject_time: dict[str, str] = field(default_factory=dict)
    consents: dict[str, ConsentRecord] = field(default_factory=dict)  # grant_id
    subjects_exited: dict[str, dict[str, Any]] = field(default_factory=dict)
    subject_responsible: dict[str, str] = field(default_factory=dict)  # subject -> 当前责任门店
    pharmacists: list[QualificationRecord] = field(default_factory=list)
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)  # plan_id
    reminders: dict[str, dict[str, Any]] = field(default_factory=dict)  # reminder_id
    facts: dict[tuple[str, str], FactRecord] = field(default_factory=dict)
    obligations: dict[str, ObligationRecord] = field(default_factory=dict)
    handoffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    withdrawn_stores: dict[str, str] = field(default_factory=dict)  # store -> event id
    known_events: dict[str, Mapping[str, Any]] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        """转为可 JSON 持久化的保存状态（集合与 dataclass 全部转基础类型）。"""
        data: dict[str, Any] = {
            "next_version": dict(self.next_version),
            "seen_event_ids": dict(self.seen_event_ids),
            "last_subject_time": dict(self.last_subject_time),
            "consents": {
                key: {**grant.__dict__, "scopes": sorted(grant.scopes)}
                for key, grant in self.consents.items()
            },
            "subjects_exited": deepcopy(self.subjects_exited),
            "subject_responsible": dict(self.subject_responsible),
            "pharmacists": [record.__dict__ for record in self.pharmacists],
            "plans": deepcopy(self.plans),
            "reminders": deepcopy(self.reminders),
            "facts": [
                {"key": list(key), **fact.__dict__} for key, fact in self.facts.items()
            ],
            "obligations": {key: record.__dict__ for key, record in self.obligations.items()},
            "handoffs": deepcopy(self.handoffs),
            "withdrawn_stores": dict(self.withdrawn_stores),
            "known_event_ids": list(self.known_events.keys()),
        }
        return data

    @classmethod
    def restore(cls, data: Mapping[str, Any]) -> "StreamState":
        data = dict(data)
        facts_data = data.pop("facts", [])
        known_event_ids = data.pop("known_event_ids", [])
        data.pop("known_events", None)
        state = cls(**data)
        state.consents = {}
        for key, value in data.get("consents", {}).items():
            if isinstance(value, ConsentRecord):
                state.consents[key] = value
            else:
                value = dict(value)
                value["scopes"] = set(value.get("scopes", ()))
                state.consents[key] = ConsentRecord(**value)
        state.pharmacists = [
            v if isinstance(v, QualificationRecord) else QualificationRecord(**v)
            for v in data.get("pharmacists", [])
        ]
        state.obligations = {
            key: (v if isinstance(v, ObligationRecord) else ObligationRecord(**v))
            for key, v in data.get("obligations", {}).items()
        }
        restored: dict[tuple[str, str], FactRecord] = {}
        for item in facts_data:
            item = dict(item)
            key = tuple(item.pop("key"))
            restored[key] = FactRecord(**item)
        state.facts = restored
        # 保存状态只保留事件标识集合；续接时用于提醒事实依据的存在性引用。
        state.known_events = {event_id: {"event_id": event_id} for event_id in data.get("known_event_ids", [])}
        return state


def _at(value: Any) -> datetime | None:
    return parse_datetime(value) if isinstance(value, str) else None


def _pharmacist_qualified(
    state: StreamState, pharmacist_id: str, store_id: str | None, at_time: str
) -> bool:
    moment = _at(at_time)
    if moment is None:
        return False
    for record in state.pharmacists:
        if record.pharmacist_id != pharmacist_id:
            continue
        if store_id is not None and record.store_id != store_id:
            continue
        if record.revoked_event_id is not None:
            continue
        start, end = _at(record.valid_from), _at(record.valid_until)
        if start is not None and end is not None and start <= moment <= end:
            return True
    return False


def _consent_active(state: StreamState, subject: str, scope: str, at_time: str) -> bool:
    moment = _at(at_time)
    if moment is None:
        return False
    for grant in state.consents.values():
        if grant.subject_id != subject or scope not in grant.scopes:
            continue
        if scope in grant.withdrawn:
            continue
        granted = _at(grant.granted_at)
        if granted is not None and granted <= moment:
            return True
    return False


def fold_events(
    events: Iterable[Mapping[str, Any]],
    state: StreamState | None = None,
) -> tuple[StreamState, list[ContractIssue]]:
    """按给定顺序折叠事件，返回新状态与全部业务问题（不抛异常、不改写输入）。"""
    state = state or StreamState()
    issues: list[ContractIssue] = []

    for index, event in enumerate(events):
        issues.extend(_apply_event(state, index, event))

    return state, issues


def _apply_event(state: StreamState, index: int, event: Mapping[str, Any]) -> list[ContractIssue]:
    event_id = str(event.get("event_id", f"#{index}"))
    duplicate_index = state.seen_event_ids.get(event_id)
    if duplicate_index is not None:
        return [
            _issue(
                index,
                event_id,
                "event_id",
                "duplicate_event_id",
                f"事件标识与 events[{duplicate_index}] 重复，重放时只生效一次",
            )
        ]

    issues: list[ContractIssue] = []
    event_type = event.get("event_type")
    aggregate_id = event.get("aggregate_id")
    occurred_at = event.get("occurred_at")
    payload = event.get("payload", {})

    if isinstance(aggregate_id, str) and isinstance(occurred_at, str):
        expected = state.next_version.get(aggregate_id, 1)
        version = event.get("version")
        if isinstance(version, int) and version != expected:
            issues.append(
                _issue(
                    index,
                    event_id,
                    "version",
                    "version_gap",
                    f"聚合 {aggregate_id} 下一版本应为 {expected}，实际为 {version}",
                )
            )
        elif isinstance(version, int):
            state.next_version[aggregate_id] = version + 1

    subject = payload.get("subject_id") if isinstance(payload, Mapping) else None
    if isinstance(subject, str) and isinstance(occurred_at, str):
        previous = state.last_subject_time.get(subject)
        if previous is not None and occurred_at < previous:
            issues.append(
                _issue(index, event_id, "occurred_at", "event_order", "同一顾客的事件时间不得倒流")
            )
        else:
            state.last_subject_time[subject] = occurred_at

    handler = _HANDLERS.get(event_type)
    if handler is not None:
        issues.extend(handler(state, index, event_id, event))

    state.seen_event_ids[event_id] = index
    state.known_events[event_id] = event
    return issues


def _guard_subject_active(
    state: StreamState, index: int, event_id: str, subject: Any, at_time: Any, code_field: str
) -> list[ContractIssue]:
    if not isinstance(subject, str) or not isinstance(at_time, str):
        return []
    exit_record = state.subjects_exited.get(subject)
    if exit_record is not None:
        exit_at, moment = _at(exit_record["effective_at"]), _at(at_time)
        if exit_at is not None and moment is not None and moment >= exit_at:
            return [
                _issue(
                    index,
                    event_id,
                    code_field,
                    "exit_frozen",
                    "顾客已退出陪伴服务，退出生效后不得再产生提醒、回访或新义务",
                )
            ]
    return []


def _handle_consent_granted(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    grant_id = event["aggregate_id"]
    if grant_id in state.consents:
        return [_issue(index, event_id, "aggregate_id", "grant_exists", "授权已记录，不得重复建档")]
    state.consents[grant_id] = ConsentRecord(
        subject_id=p["subject_id"],
        grant_event_id=event_id,
        store_id=p["store_id"],
        scopes=set(p["scopes"]),
        granted_at=p["granted_at"],
    )
    return []


def _handle_consent_withdrawn(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    grant = state.consents.get(p["grant_id"])
    if grant is None:
        return [_issue(index, event_id, "payload.grant_id", "unknown_grant", "撤回必须指向已记录的分项授权")]
    issues = []
    for scope in p["scopes_withdrawn"]:
        if scope not in grant.scopes:
            issues.append(
                _issue(index, event_id, "payload.scopes_withdrawn", "scope_not_granted", f"授权项 {scope} 从未授予")
            )
        elif scope in grant.withdrawn:
            issues.append(
                _issue(index, event_id, "payload.scopes_withdrawn", "scope_already_withdrawn", f"授权项 {scope} 已撤回")
            )
        else:
            grant.withdrawn[scope] = event_id
    return issues


def _handle_qualified(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    if p["valid_from"] >= p["valid_until"]:
        return [_issue(index, event_id, "payload.valid_until", "invalid_window", "资质失效时间必须晚于生效时间")]
    state.pharmacists.append(
        QualificationRecord(
            pharmacist_id=p["pharmacist_id"],
            store_id=p["store_id"],
            valid_from=p["valid_from"],
            valid_until=p["valid_until"],
        )
    )
    return []


def _handle_shift(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    if p["shift_start"] >= p["shift_end"]:
        return [_issue(index, event_id, "payload.shift_end", "invalid_window", "班次结束时间必须晚于开始时间")]
    if not _pharmacist_qualified(state, p["pharmacist_id"], p["store_id"], p["shift_start"]):
        return [
            _issue(
                index,
                event_id,
                "payload.pharmacist_id",
                "qualification_required",
                "排班前药师须在该门店持有有效资质",
            )
        ]
    return []


def _handle_qualification_revoked(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    for record in state.pharmacists:
        if record.pharmacist_id == p["pharmacist_id"] and record.store_id == p["store_id"]:
            record.revoked_event_id = event_id
            return []
    return [_issue(index, event_id, "payload.pharmacist_id", "unknown_qualification", "撤销资质须有在先有效资质记录")]


def _handle_source_declared(state, index, event_id, event) -> list[ContractIssue]:
    return []  # 处方/自购来源声明仅需形态正确，不作为服务门槛


def _handle_plan_activated(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    issues: list[ContractIssue] = []
    if p["plan_version"] != 1:
        issues.append(_issue(index, event_id, "payload.plan_version", "version_mismatch", "首次激活的计划版本必须为 1"))
    if p["plan_id"] in state.plans:
        issues.append(_issue(index, event_id, "payload.plan_id", "plan_exists", "计划已激活，不得重复激活"))
    if not _consent_active(state, p["subject_id"], "care_profile", p["activated_at"]):
        issues.append(_issue(index, event_id, "payload.subject_id", "consent_required", "激活健康计划须先取得建档授权"))
    if not _pharmacist_qualified(state, p["pharmacist_id"], p["store_id"], p["activated_at"]):
        issues.append(_issue(index, event_id, "payload.pharmacist_id", "qualification_required", "激活计划时药师资质须有效"))
    if issues:
        return issues
    state.plans[p["plan_id"]] = {
        "plan_id": p["plan_id"],
        "subject_id": p["subject_id"],
        "version": 1,
        "store_id": p["store_id"],
        "pharmacist_id": p["pharmacist_id"],
        "activated_event_id": event_id,
    }
    state.subject_responsible.setdefault(p["subject_id"], p["store_id"])
    return []


def _handle_plan_revised(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    plan = state.plans.get(p["plan_id"])
    issues: list[ContractIssue] = []
    if plan is None:
        issues.append(_issue(index, event_id, "payload.plan_id", "unknown_plan", "修订计划须先激活"))
    else:
        if plan["subject_id"] != p["subject_id"]:
            issues.append(_issue(index, event_id, "payload.subject_id", "subject_mismatch", "计划归属顾客不一致"))
        if p["supersedes_version"] != plan["version"]:
            issues.append(
                _issue(index, event_id, "payload.supersedes_version", "version_mismatch", "修订必须衔接当前计划版本")
            )
        if p["plan_version"] != plan["version"] + 1:
            issues.append(_issue(index, event_id, "payload.plan_version", "version_mismatch", "新版本号必须逐 1 递增"))
    if not _pharmacist_qualified(state, p["pharmacist_id"], None, p["revised_at"]):
        issues.append(_issue(index, event_id, "payload.pharmacist_id", "qualification_required", "修订计划时药师资质须有效"))
    if issues:
        return issues
    plan["version"] = p["plan_version"]
    plan["pharmacist_id"] = p["pharmacist_id"]
    plan["basis_event_id"] = event_id
    return []


def _plan_version_active(state, plan_id: str, version: int) -> bool:
    plan = state.plans.get(plan_id)
    return plan is not None and 1 <= version <= plan["version"]


def _handle_reminder(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    issues = _guard_subject_active(state, index, event_id, p.get("subject_id"), p.get("fired_at"), "payload.subject_id")
    if not _consent_active(state, p["subject_id"], "medication_reminder", p["fired_at"]):
        issues.append(_issue(index, event_id, "payload.subject_id", "consent_required", "发送用药提醒须有分项提醒授权"))
    if not _plan_version_active(state, p["plan_id"], p["plan_version"]):
        issues.append(_issue(index, event_id, "payload.plan_version", "unknown_plan_version", "提醒必须基于已生效的计划版本"))
    basis = p.get("factual_basis")
    if not isinstance(basis, Mapping) or not basis.get("based_on_event_id"):
        issues.append(_issue(index, event_id, "payload.factual_basis", "basis_required", "每次提醒必须给出事实依据（based_on_event_id）"))
    elif basis["based_on_event_id"] not in state.known_events:
        issues.append(_issue(index, event_id, "payload.factual_basis", "unknown_basis_event", "提醒事实依据指向不存在的事件"))
    if p["window_start"] >= p["window_end"]:
        issues.append(_issue(index, event_id, "payload.window_end", "invalid_window", "回访窗口结束时间必须晚于开始时间"))
    withdrawn_at = state.withdrawn_stores.get(p["store_id"])
    if withdrawn_at is not None and _at(withdrawn_at) is not None and _at(withdrawn_at) <= _at(p["fired_at"]):
        issues.append(_issue(index, event_id, "payload.store_id", "store_withdrawn", "门店停业生效后不得新开提醒义务"))
    obligation_id = p["obligation_id"]
    if obligation_id in state.obligations:
        issues.append(_issue(index, event_id, "payload.obligation_id", "obligation_exists", "提醒义务标识重复"))
    else:
        state.obligations[obligation_id] = ObligationRecord(
            obligation_id=obligation_id,
            subject_id=p["subject_id"],
            owner_store=p["store_id"],
            opened_event_id=event_id,
        )
    state.reminders[p["reminder_id"]] = {
        "subject_id": p["subject_id"],
        "store_id": p["store_id"],
        "plan_id": p["plan_id"],
        "plan_version": p["plan_version"],
        "obligation_id": obligation_id,
        "window_start": p["window_start"],
        "window_end": p["window_end"],
        "factual_basis": dict(p["factual_basis"]),
        "fired_event_id": event_id,
        "fired_at": p["fired_at"],
    }
    return issues


def _handle_followup(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    issues = _guard_subject_active(state, index, event_id, p.get("subject_id"), p.get("recorded_at"), "payload.subject_id")
    if not _consent_active(state, p["subject_id"], "adherence_followup", p["recorded_at"]):
        issues.append(_issue(index, event_id, "payload.subject_id", "consent_required", "记录依从回访须有回访授权"))
    key = (p["subject_id"], p["fact_key"])
    fact = state.facts.get(key)
    if fact is None:
        fact = FactRecord(fact_key=p["fact_key"], subject_id=p["subject_id"])
        state.facts[key] = fact
    for prior in fact.recordings:
        if prior["content_hash"] != p["content_hash"]:
            fact.conflicting = True
    fact.recordings.append(
        {
            "store_id": p["store_id"],
            "content_hash": p["content_hash"],
            "recorded_by": p["recorded_by"],
            "event_id": event_id,
        }
    )
    return issues


def _handle_service_confirmed(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    issues: list[ContractIssue] = []
    fact = state.facts.get((p["subject_id"], p["fact_key"]))
    if fact is None:
        issues.append(_issue(index, event_id, "payload.fact_key", "unknown_fact", "签认前须先记录服务事实"))
    elif fact.conflicting and fact.verification is None:
        issues.append(
            _issue(index, event_id, "payload.fact_key", "unverified_conflict", "矛盾事实须先经独立药师核验后方可签认")
        )
    if p["personally_performed"] is not True:
        issues.append(_issue(index, event_id, "payload.personally_performed", "personal_attestation_required", "药师只可签认亲自完成的专业服务"))
    if not _pharmacist_qualified(state, p["pharmacist_id"], p["store_id"], p["confirmed_at"]):
        issues.append(_issue(index, event_id, "payload.pharmacist_id", "qualification_required", "签认时药师在该门店的资质须有效"))
    obligation = state.obligations.get(p["obligation_id"])
    if obligation is None:
        issues.append(_issue(index, event_id, "payload.obligation_id", "unknown_obligation", "签认须对应一条已开启的义务"))
    elif obligation.subject_id != p["subject_id"]:
        issues.append(_issue(index, event_id, "payload.obligation_id", "subject_mismatch", "义务归属顾客不一致"))
    elif obligation.status == "fulfilled":
        issues.append(_issue(index, event_id, "payload.obligation_id", "obligation_closed", "义务已履行，不得重复签认"))
    if issues:
        return issues
    obligation.status = "fulfilled"
    return []


def _handle_conflict_reported(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    fact = state.facts.get((p["subject_id"], p["fact_key"]))
    if fact is None:
        return [_issue(index, event_id, "payload.fact_key", "unknown_fact", "上报矛盾须先存在服务事实")]
    fact.conflicting = True
    return []


def _handle_fact_verified(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    fact = state.facts.get((p["subject_id"], p["fact_key"]))
    issues: list[ContractIssue] = []
    if fact is None:
        issues.append(_issue(index, event_id, "payload.fact_key", "unknown_fact", "核验须针对已记录的服务事实"))
    if not fact or not fact.conflicting:
        issues.append(_issue(index, event_id, "payload.fact_key", "no_conflict", "仅矛盾事实需要独立核验"))
    if p["verifier_sales_independent"] is not True:
        issues.append(_issue(index, event_id, "payload.verifier_sales_independent", "sales_independence_required", "核验药师必须未参与销售"))
    if not _pharmacist_qualified(state, p["verifier_id"], None, p["verified_at"]):
        issues.append(_issue(index, event_id, "payload.verifier_id", "qualification_required", "核验药师资质须有效"))
    participants = {r["recorded_by"] for r in fact.recordings} if fact else set()
    if p["verifier_id"] in participants:
        issues.append(_issue(index, event_id, "payload.verifier_id", "verifier_conflict", "核验药师不得是该事实的记录人"))
    if issues:
        return issues
    fact.verification = {"verifier_id": p["verifier_id"], "resolution": p["resolution"], "event_id": event_id}
    if p["resolution"] == "voided":
        fact.conflicting = False
    return []


def _handle_escalated(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    obligation = state.obligations.get(p["obligation_id"])
    if obligation is None:
        return [_issue(index, event_id, "payload.obligation_id", "unknown_obligation", "异常升级须对应一条已开启的义务")]
    if obligation.status == "fulfilled":
        return [_issue(index, event_id, "payload.obligation_id", "obligation_closed", "义务已履行，无需升级")]
    obligation.escalated_event_id = event_id
    return []


def _handle_store_withdrawn(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    if p["store_id"] in state.withdrawn_stores:
        return [_issue(index, event_id, "payload.store_id", "store_already_withdrawn", "门店已登记停业")]
    state.withdrawn_stores[p["store_id"]] = p["effective_at"]
    return []


def _handle_handoff_proposed(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    if p["handoff_id"] in state.handoffs:
        return [_issue(index, event_id, "payload.handoff_id", "handoff_exists", "交接提案已存在")]
    state.handoffs[p["handoff_id"]] = {"proposal": p, "completed": False}
    return []


def _handle_handoff_completed(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    handoff = state.handoffs.get(p["handoff_id"])
    issues: list[ContractIssue] = []
    if handoff is None:
        issues.append(_issue(index, event_id, "payload.handoff_id", "unknown_handoff", "完成交接前须先提出交接提案"))
    elif handoff["completed"]:
        issues.append(_issue(index, event_id, "payload.handoff_id", "handoff_completed", "交接已完成，不得重复接管"))
    else:
        proposal = handoff["proposal"]
        for key in ("subject_id", "obligation_ids", "from_store", "to_store"):
            if proposal.get(key) != p.get(key):
                issues.append(_issue(index, event_id, f"payload.{key}", "handoff_mismatch", "完成内容与交接提案不一致"))
    if not _consent_active(state, p["subject_id"], "cross_store_handoff", p["completed_at"]):
        issues.append(_issue(index, event_id, "payload.subject_id", "consent_required", "跨店承接须有跨店交接授权"))
    current_owner: dict[str, str] = {}
    for obligation_id in p["obligation_ids"]:
        obligation = state.obligations.get(obligation_id)
        if obligation is None:
            issues.append(_issue(index, event_id, "payload.obligation_ids", "unknown_obligation", f"义务 {obligation_id} 不存在"))
            continue
        if obligation.subject_id != p["subject_id"]:
            issues.append(_issue(index, event_id, "payload.obligation_ids", "subject_mismatch", f"义务 {obligation_id} 归属顾客不一致"))
        if obligation.status == "fulfilled":
            issues.append(
                _issue(index, event_id, "payload.obligation_ids", "obligation_closed", f"已履行义务 {obligation_id} 不随交接迁移")
            )
        current_owner[obligation_id] = obligation.owner_store
    for obligation_id, owner in current_owner.items():
        if owner != p["from_store"]:
            issues.append(
                _issue(
                    index,
                    event_id,
                    "payload.obligation_ids",
                    "responsibility_taken",
                    f"义务 {obligation_id} 已由 {owner} 承接，竞争接管中仅一家可成功",
                )
            )
    if issues:
        return issues
    for obligation_id in p["obligation_ids"]:
        state.obligations[obligation_id].owner_store = p["to_store"]
    handoff["completed"] = True
    handoff["completed_event_id"] = event_id
    state.subject_responsible[p["subject_id"]] = p["to_store"]
    return []


def _handle_stock(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    issues: list[ContractIssue] = []
    if not _consent_active(state, p["subject_id"], "emergency_supply", p["committed_at"]):
        issues.append(_issue(index, event_id, "payload.subject_id", "consent_required", "应急供应承诺须有应急供应授权"))
    obligation = state.obligations.get(p["obligation_id"])
    if obligation is None:
        issues.append(_issue(index, event_id, "payload.obligation_id", "unknown_obligation", "库存承诺须对应一条未关闭义务"))
    elif obligation.status == "fulfilled":
        issues.append(_issue(index, event_id, "payload.obligation_id", "obligation_closed", "义务已履行，无需应急库存承诺"))
    if issues:
        return issues
    obligation.stock_event_id = event_id
    return []


def _handle_exit(state, index, event_id, event) -> list[ContractIssue]:
    p = event["payload"]
    if p["subject_id"] in state.subjects_exited:
        return [_issue(index, event_id, "payload.subject_id", "exit_exists", "顾客已作出退出决定")]
    if p["legal_proof_retained"] is not True:
        return [
            _issue(
                index,
                event_id,
                "payload.legal_proof_retained",
                "legal_proof_required",
                "退出后仍须保留并可出具法定购药与服务证明",
            )
        ]
    state.subjects_exited[p["subject_id"]] = {"effective_at": p["effective_at"], "event_id": event_id}
    return []


_HANDLERS = {
    "CONSENT_GRANTED": _handle_consent_granted,
    "CONSENT_WITHDRAWN": _handle_consent_withdrawn,
    "PHARMACIST_QUALIFIED": _handle_qualified,
    "PHARMACIST_SHIFT_SCHEDULED": _handle_shift,
    "QUALIFICATION_REVOKED": _handle_qualification_revoked,
    "MEDICATION_SOURCE_DECLARED": _handle_source_declared,
    "PLAN_ACTIVATED": _handle_plan_activated,
    "PLAN_REVISED": _handle_plan_revised,
    "REMINDER_FIRED": _handle_reminder,
    "FOLLOWUP_RECORDED": _handle_followup,
    "SERVICE_CONFIRMED": _handle_service_confirmed,
    "FACT_CONFLICT_REPORTED": _handle_conflict_reported,
    "FACT_VERIFIED": _handle_fact_verified,
    "ANOMALY_ESCALATED": _handle_escalated,
    "STORE_WITHDRAWN": _handle_store_withdrawn,
    "HANDOFF_PROPOSED": _handle_handoff_proposed,
    "HANDOFF_COMPLETED": _handle_handoff_completed,
    "EMERGENCY_STOCK_COMMITTED": _handle_stock,
    "CARE_EXITED": _handle_exit,
}


def validate_stream(events: Iterable[Mapping[str, Any]]) -> tuple[StreamState, list[ContractIssue]]:
    """校验整条事件流，返回折叠状态与稳定排序的问题列表。"""
    events = list(events)
    state, issues = fold_events(events)
    return state, sorted(issues, key=lambda issue: (issue.field, issue.code))
