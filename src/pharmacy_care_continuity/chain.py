"""药店健康陪伴责任链：事件溯源的上层业务服务。

交换层（``contracts.py``）只校验信封、枚举、时间、版本与必填载荷；
本模块负责叙述中的业务规则：

- 分项授权（按 scope 授权/撤回，撤回额外用途不影响法定购药与服务证明）；
- 药师资质与班次（只有资质有效、当班且本人完成，才能签认专业服务）；
- 处方/自购来源声明；
- 服务计划版本（销售优惠不得改变健康计划，系统不自动诊断）；
- 依从回访：跨店同一 ``fact_key`` 归并为一次服务事实，矛盾由未参与销售的药师核验；
- 异常升级（缺药等）；
- 门店承接：未来义务同一时刻只能有一家门店接手成功；
- 应急库存承诺；
- 退出决定；
- 门店关闭或资质失效只迁移尚未履行的义务；
- 普通店员最小访问；
- 崩溃恢复：所有决策只依赖已保存事件，交接中断后按保存状态继续。

时间一律使用带时区的 ``datetime``。命令在成功前先产出事件，再原子加入日志，
调用方可在加入前把事件持久化，从而保证"按保存状态继续"。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable, Iterable, Mapping, Sequence

from .contracts import ContractIssue, validate_event

# 授权分项。法定购药与服务证明不属于额外数据用途，始终保留。
SCOPE_REMINDER = "medication_reminder"
SCOPE_FOLLOWUP = "adherence_followup"
SCOPE_HANDOFF = "cross_store_handoff"
SCOPE_MARKETING = "marketing_analytics"
EXTRA_SCOPES = frozenset({SCOPE_REMINDER, SCOPE_FOLLOWUP, SCOPE_HANDOFF, SCOPE_MARKETING})
SCOPE_LABELS = {
    SCOPE_REMINDER: "用药提醒",
    SCOPE_FOLLOWUP: "依从回访",
    SCOPE_HANDOFF: "跨店承接",
    SCOPE_MARKETING: "营销分析",
}

# 专业服务事实类型与所需授权分项。
FACT_KIND_REMINDER = "medication_reminder"
FACT_KIND_FOLLOWUP = "adherence_followup"
FACT_KINDS = frozenset({FACT_KIND_REMINDER, FACT_KIND_FOLLOWUP})
FACT_SCOPE = {
    FACT_KIND_REMINDER: SCOPE_REMINDER,
    FACT_KIND_FOLLOWUP: SCOPE_FOLLOWUP,
}

DIAGNOSIS_NOTICE = "系统不自动作出诊断，本记录不构成诊断结论"
ROLE_PHARMACIST = "pharmacist"
ROLE_STAFF = "staff"

# 回访结论；矛盾指同一事实出现互斥结论。
OUTCOME_TAKEN = "taken"
OUTCOME_MISSED = "missed"
OUTCOME_CONTRADICTS = {OUTCOME_TAKEN: OUTCOME_MISSED, OUTCOME_MISSED: OUTCOME_TAKEN}

REASON_STORE_CLOSURE = "store_closure"
REASON_QUALIFICATION_LAPSE = "qualification_lapse"

# 角色可见的健康字段（普通店员最小访问）。
ROLE_VISIBLE_FIELDS = {
    ROLE_PHARMACIST: frozenset(
        {
            "customer_id",
            "scopes",
            "plan",
            "sources",
            "obligations",
            "facts",
            "escalations",
            "handoffs",
            "stock_commitments",
            "exit",
        }
    ),
    ROLE_STAFF: frozenset({"customer_id"}),
}


class ChainError(Exception):
    """业务规则冲突；该错误下不会产生任何事件。"""


@dataclass(frozen=True)
class Obligation:
    """一项未来必须履行的服务义务（如某轮用药提醒/依从回访）。"""

    key: str
    fact_kind: str
    window_start: datetime
    window_end: datetime
    store_id: str
    pharmacist_id: str | None
    plan_id: str
    plan_version: int
    status: str = "open"  # open / confirmed / escalated / transferred / cancelled / superseded


@dataclass(frozen=True)
class Confirmation:
    event_id: str
    store_id: str
    pharmacist_id: str
    sales_offer_id: str | None
    outcome: str
    notes: str
    confirmed_at: datetime


@dataclass(frozen=True)
class ServiceFact:
    """跨店归并后的一次服务事实。"""

    key: str
    fact_kind: str
    customer_id: str
    confirmations: tuple[Confirmation, ...] = ()
    resolved_outcome: str | None = None
    resolver_pharmacist_id: str | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True)
class Escalation:
    key: str
    reason_code: str
    store_id: str
    pharmacist_id: str
    window_start: datetime
    window_end: datetime
    status: str  # open / resolved
    evidence_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class Handoff:
    handoff_id: str
    customer_id: str
    from_store: str
    to_store: str
    obligation_keys: frozenset[str]
    status: str  # accepted / rejected / completed
    plan_id: str
    plan_version: int


@dataclass(frozen=True)
class StockCommitment:
    commitment_id: str
    medication_key: str
    store_id: str
    quantity: int
    valid_until: datetime
    status: str = "committed"  # committed / consumed / expired


@dataclass(frozen=True)
class PlanView:
    plan_id: str
    plan_version: int
    store_id: str
    pharmacist_id: str
    obligations: tuple[Obligation, ...]
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True)
class ResponsibilityView:
    """受限接口返回：当前责任人、采用计划版本、每次提醒的事实依据。"""

    customer_id: str
    responsible_store: str | None
    responsible_pharmacist: str | None
    plan: PlanView | None
    reminders: tuple[Mapping[str, Any], ...]
    diagnosis_notice: str


def _parse_dt(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ChainError("时间必须携带时区")
    return dt


def _envelope(event_type: str, aggregate_type: str, aggregate_id: str,
              occurred_at: datetime, version: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": f"{event_type.lower()}-{aggregate_id}-v{version}",
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at.isoformat(),
        "version": version,
        "payload": dict(payload),
    }


@dataclass
class ChainState:
    """由已保存事件重放出的责任链状态。重建后行为与中断前一致。"""

    schema: Mapping[str, Any]
    scopes: dict[str, bool] = field(default_factory=dict)
    qualifications: dict[str, dict[str, Any]] = field(default_factory=dict)  # pharmacist -> dict
    shifts: dict[str, dict[str, Any]] = field(default_factory=dict)          # pharmacist -> {store,until}
    plans: dict[str, dict[str, Any]] = field(default_factory=dict)           # plan_id -> latest plan record
    sources: list[dict[str, Any]] = field(default_factory=list)
    obligations: dict[str, Obligation] = field(default_factory=dict)
    facts: dict[str, ServiceFact] = field(default_factory=dict)
    escalations: dict[str, Escalation] = field(default_factory=dict)
    handoffs: dict[str, Handoff] = field(default_factory=dict)
    stocks: dict[str, StockCommitment] = field(default_factory=dict)
    exit: dict[str, Any] | None = None
    proofs: list[dict[str, Any]] = field(default_factory=list)
    stores_withdrawn: dict[str, datetime] = field(default_factory=dict)
    # 事件幂等：event_id 只接受一次；每个聚合的版本号必须连续递增。
    seen_event_ids: set[str] = field(default_factory=set)
    versions: dict[str, int] = field(default_factory=dict)
    log: list[Mapping[str, Any]] = field(default_factory=list)


# --- 内部判定 ---------------------------------------------------------------


def _now_of(event: Mapping[str, Any]) -> datetime:
    return _parse_dt(event["occurred_at"])


def _is_pharmacist_qualified(state: ChainState, pharmacist_id: str, at: datetime) -> bool:
    record = state.qualifications.get(pharmacist_id)
    return bool(
        record
        and _parse_dt(record["valid_from"]) <= at < _parse_dt(record["valid_until"])
    )


def _is_on_duty(state: ChainState, pharmacist_id: str, store_id: str, at: datetime) -> bool:
    shift = state.shifts.get(pharmacist_id)
    return bool(
        shift
        and shift["store_id"] == store_id
        and _parse_dt(shift["valid_from"]) <= at < _parse_dt(shift["valid_until"])
    )


def _require_pharmacist(state: ChainState, pharmacist_id: str, store_id: str, at: datetime) -> None:
    if not _is_pharmacist_qualified(state, pharmacist_id, at):
        raise ChainError(f"药师 {pharmacist_id} 资质缺失或已失效，不能签认专业服务")
    if not _is_on_duty(state, pharmacist_id, store_id, at):
        raise ChainError(f"药师 {pharmacist_id} 不在 {store_id} 的有效班次内")


def _require_scope(state: ChainState, scope: str) -> None:
    if not state.scopes.get(scope):
        raise ChainError(f"顾客未授权或已撤回分项：{SCOPE_LABELS.get(scope, scope)}")


def _plan_of(state: ChainState, plan_id: str, version: int | None = None) -> dict[str, Any]:
    plan = state.plans.get(plan_id)
    if plan is None:
        raise ChainError(f"服务计划 {plan_id} 不存在")
    if version is not None and plan["plan_version"] != version:
        raise ChainError(
            f"计划版本 {version} 非当前版本（当前 v{plan['plan_version']}）"
        )
    return plan


def _obligation_open(state: ChainState, key: str) -> Obligation:
    obligation = state.obligations.get(key)
    if obligation is None:
        raise ChainError(f"义务 {key} 不存在")
    if obligation.status != "open":
        raise ChainError(f"义务 {key} 已 {obligation.status}，不可重复处理")
    return obligation


# --- 应用事件（纯状态演进；只处理通过交换层校验的事件） -----------------------


def apply_event(state: ChainState, event: Mapping[str, Any]) -> None:
    issues = validate_event(event, state.schema)
    if issues:
        raise ChainError("事件未通过交换层校验：" + "; ".join(i.code for i in issues))
    if event["event_id"] in state.seen_event_ids:
        raise ChainError(f"事件 {event['event_id']} 已保存，重复事件被忽略")

    # 版本号在每个聚合上必须从 1 开始连续递增；重放据此复原，且能挡住并发乱序提交。
    expected_version = state.versions.get(event["aggregate_id"], 0) + 1
    if event["version"] != expected_version:
        raise ChainError(
            f"聚合 {event['aggregate_id']} 版本应为 {expected_version}，收到 {event['version']}"
        )

    kind = event["event_type"]
    p = event["payload"]
    at = _now_of(event)

    if kind == "CONSENT_RECORDED":
        for scope in p["scopes"]:
            state.scopes[scope] = bool(p["granted"])
    elif kind == "CONSENT_SCOPE_WITHDRAWN":
        state.scopes[p["scope"]] = False
        # 派生效应：撤回额外数据用途后，取消尚未履行、且仅依赖该分项的义务；
        # 法定购药与服务证明不在此列。该效应在重放时必须可复现。
        for key, obligation in list(state.obligations.items()):
            if obligation.status == "open" and FACT_SCOPE[obligation.fact_kind] == p["scope"]:
                state.obligations[key] = replace(obligation, status="cancelled")
    elif kind == "PHARMACIST_QUALIFICATION_REGISTERED":
        state.qualifications[p["pharmacist_id"]] = {
            "license_no": p["license_no"],
            "valid_from": p["valid_from"],
            "valid_until": p["valid_until"],
        }
    elif kind == "PHARMACIST_SHIFT_OPENED":
        state.shifts[p["pharmacist_id"]] = {
            "store_id": p["store_id"],
            "valid_from": p["valid_from"],
            "valid_until": p["valid_until"],
        }
    elif kind == "PHARMACIST_SHIFT_CLOSED":
        shift = state.shifts.get(p["pharmacist_id"])
        if shift and shift["store_id"] == p["store_id"]:
            shift["valid_until"] = p["effective_at"]
    elif kind == "SOURCE_DECLARED":
        state.sources.append(dict(p))
    elif kind in ("PLAN_ACTIVATED", "PLAN_SUPPLANTED"):
        # 换版：同一计划上一版本尚未履行的义务自动作废，只保留新版义务。
        if kind == "PLAN_SUPPLANTED":
            for key, obligation in list(state.obligations.items()):
                if obligation.plan_id == p["plan_id"] and obligation.status == "open":
                    state.obligations[key] = replace(obligation, status="superseded")
        state.plans[p["plan_id"]] = {
            "plan_version": p["plan_version"],
            "store_id": p["store_id"],
            "pharmacist_id": p["pharmacist_id"],
            "obligation_keys": [o["key"] for o in p["obligations"]],
            "valid_from": p["valid_from"],
            "valid_until": p["valid_until"],
        }
        for o in p["obligations"]:
            state.obligations[o["key"]] = Obligation(
                key=o["key"],
                fact_kind=o["fact_kind"],
                window_start=_parse_dt(o["window_start"]),
                window_end=_parse_dt(o["window_end"]),
                store_id=p["store_id"],
                pharmacist_id=p["pharmacist_id"],
                plan_id=p["plan_id"],
                plan_version=p["plan_version"],
            )
    elif kind == "REMINDER_DUE_CALLED":
        # 呼叫即形成一次服务事实（尚无结论）；跨店重复回访凭 fact_key 归入同一事实。
        if p["fact_key"] not in state.facts:
            state.facts[p["fact_key"]] = ServiceFact(
                key=p["fact_key"], fact_kind=p["fact_kind"], customer_id=p["customer_id"]
            )
    elif kind == "SERVICE_CONFIRMED":
        confirmation = Confirmation(
            event_id=event["event_id"],
            store_id=p["store_id"],
            pharmacist_id=p["pharmacist_id"],
            sales_offer_id=p["sales_offer_id"],
            outcome=p["outcome"],
            notes=p["notes"],
            confirmed_at=_parse_dt(p["confirmed_at"]),
        )
        fact = state.facts.get(p["fact_key"])
        if fact is None:
            fact = ServiceFact(key=p["fact_key"], fact_kind=p["fact_kind"], customer_id=p["customer_id"])
        state.facts[p["fact_key"]] = replace(fact, confirmations=fact.confirmations + (confirmation,))
        obligation = state.obligations.get(p["obligation_key"]) if "obligation_key" in p else None
        if obligation is not None and obligation.status == "open":
            state.obligations[obligation.key] = replace(obligation, status="confirmed")
    elif kind == "DISCREPANCY_RESOLVED":
        fact = state.facts[p["fact_key"]]
        state.facts[p["fact_key"]] = replace(
            fact,
            resolved_outcome=next(
                c.outcome for c in fact.confirmations if c.event_id == p["chosen_confirmation_id"]
            ),
            resolver_pharmacist_id=p["verifier_pharmacist_id"],
            resolved_at=_parse_dt(p["resolved_at"]),
        )
    elif kind == "EXCEPTION_ESCALATED":
        obligation = state.obligations.get(p["obligation_key"]) if "obligation_key" in p else None
        if obligation is not None and obligation.status == "open":
            state.obligations[obligation.key] = replace(obligation, status="escalated")
        state.escalations[p["fact_key"]] = Escalation(
            key=p["fact_key"],
            reason_code=p["reason_code"],
            store_id=p["store_id"],
            pharmacist_id=p["pharmacist_id"],
            window_start=_parse_dt(p["window_start"]),
            window_end=_parse_dt(p["window_end"]),
            status="open",
            evidence_event_ids=tuple(p["evidence_event_ids"]),
        )
    elif kind == "STORE_WITHDRAWN":
        state.stores_withdrawn[p["store_id"]] = _parse_dt(p["effective_at"])
    elif kind == "HANDOFF_ACCEPTED":
        keys = frozenset(p["obligation_keys"])
        state.handoffs[p["handoff_id"]] = Handoff(
            handoff_id=p["handoff_id"],
            customer_id=p["customer_id"],
            from_store=p["from_store"],
            to_store=p["to_store"],
            obligation_keys=keys,
            status="accepted",
            plan_id=p["plan_id"],
            plan_version=p["plan_version"],
        )
        # 承接成功只更换责任门店；义务仍未履行（保持 open/escalated），新店据此继续。
        # 一旦责任迁走，其 store_id 不再是退出门店，后到的承接必然落空——天然唯一。
        for key in keys:
            obligation = state.obligations[key]
            state.obligations[key] = replace(
                obligation, store_id=p["to_store"], pharmacist_id=p["accepting_pharmacist_id"]
            )
    elif kind == "HANDOFF_REJECTED":
        state.handoffs[p["handoff_id"]] = Handoff(
            handoff_id=p["handoff_id"],
            customer_id=p.get("customer_id", ""),
            from_store=p["from_store"],
            to_store=p["to_store"],
            obligation_keys=frozenset(),
            status="rejected",
            plan_id=p.get("plan_id", ""),
            plan_version=p.get("plan_version", 0),
        )
    elif kind == "HANDOFF_COMPLETED":
        handoff = state.handoffs[p["handoff_id"]]
        state.handoffs[p["handoff_id"]] = replace(handoff, status="completed")
    elif kind == "EMERGENCY_STOCK_COMMITTED":
        state.stocks[p["commitment_id"]] = StockCommitment(
            commitment_id=p["commitment_id"],
            medication_key=p["medication_key"],
            store_id=p["store_id"],
            quantity=p["quantity"],
            valid_until=_parse_dt(p["valid_until"]),
        )
    elif kind == "EXIT_DECIDED":
        state.exit = dict(p)
        for key, obligation in list(state.obligations.items()):
            if obligation.status == "open":
                state.obligations[key] = replace(obligation, status="cancelled")
    elif kind == "LEGAL_PROOF_ISSUED":
        state.proofs.append(dict(p))

    state.seen_event_ids.add(event["event_id"])
    state.versions[event["aggregate_id"]] = event["version"]
    state.log.append(event)


# --- 责任链服务（命令） ------------------------------------------------------


class ResponsibilityChain:
    """基于事件日志的责任链应用服务。

    所有命令先通过业务校验产出事件，再交给 ``append`` 持久化并重放。
    ``sink`` 是事件保存回调（如写库/发消息）；抛异常则该事件不进入状态，
    从而交接中断后可严格按已保存状态继续。
    """

    def __init__(self, schema: Mapping[str, Any], history: Iterable[Mapping[str, Any]] = (),
                 sink: Callable[[Mapping[str, Any]], None] | None = None) -> None:
        self.state = ChainState(schema=schema)
        self.sink = sink
        for event in history:
            apply_event(self.state, event)

    def append(self, event: Mapping[str, Any]) -> None:
        """持久化并应用；sink 失败时状态不变（崩溃恢复边界）。"""
        if self.sink is not None:
            self.sink(event)
        apply_event(self.state, event)

    # 授权与人员 -------------------------------------------------------------

    def record_consent(self, customer_id: str, scopes: Sequence[str], granted: bool,
                       recorded_by: str, at: datetime) -> None:
        unknown = [s for s in scopes if s not in EXTRA_SCOPES]
        if unknown:
            raise ChainError(f"未登记的授权分项：{unknown}")
        event = _envelope(
            "CONSENT_RECORDED", "consent_grant", f"consent-{customer_id}", at,
            self._next_version(f"consent-{customer_id}"),
            {
                "customer_id": customer_id,
                "scopes": list(scopes),
                "granted": granted,
                "recorded_by": recorded_by,
            },
        )
        self.append(event)

    def withdraw_scope(self, customer_id: str, scope: str, withdrawn_by: str, at: datetime) -> None:
        if scope not in EXTRA_SCOPES:
            raise ChainError(f"未登记的授权分项：{scope}")
        event = _envelope(
            "CONSENT_SCOPE_WITHDRAWN", "consent_grant", f"consent-{customer_id}", at,
            self._next_version(f"consent-{customer_id}"),
            {
                "customer_id": customer_id,
                "scope": scope,
                "withdrawn_by": withdrawn_by,
                "withdrawn_at": at.isoformat(),
            },
        )
        self.append(event)
        # 撤回额外用途对未履行义务的取消由 apply_event 派生，保证重建后可复现。

    def register_qualification(self, pharmacist_id: str, license_no: str,
                               valid_from: datetime, valid_until: datetime,
                               registered_by: str, at: datetime) -> None:
        if valid_until <= valid_from:
            raise ChainError("资质失效时间必须晚于生效时间")
        event = _envelope(
            "PHARMACIST_QUALIFICATION_REGISTERED", "pharmacist_directory",
            f"pharmacist-{pharmacist_id}", at,
            self._next_version(f"pharmacist-{pharmacist_id}"),
            {
                "pharmacist_id": pharmacist_id,
                "license_no": license_no,
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
                "registered_by": registered_by,
            },
        )
        self.append(event)

    def open_shift(self, pharmacist_id: str, store_id: str,
                   valid_from: datetime, valid_until: datetime) -> None:
        if valid_until <= valid_from:
            raise ChainError("班次结束时间必须晚于开始时间")
        event = _envelope(
            "PHARMACIST_SHIFT_OPENED", "pharmacist_assignment",
            f"shift-{pharmacist_id}-{store_id}", valid_from,
            self._next_version(f"shift-{pharmacist_id}-{store_id}"),
            {
                "pharmacist_id": pharmacist_id,
                "store_id": store_id,
                "valid_from": valid_from.isoformat(),
                "valid_until": valid_until.isoformat(),
            },
        )
        self.append(event)

    # 来源声明与计划 ---------------------------------------------------------

    def declare_source(self, customer_id: str, medication_key: str, source_kind: str,
                       declared_by: str, at: datetime) -> None:
        if source_kind not in ("prescription", "self_purchase"):
            raise ChainError("来源只能是 prescription（处方）或 self_purchase（自购）")
        aggregate_id = f"source-{customer_id}-{medication_key}"
        event = _envelope(
            "SOURCE_DECLARED", "medication_source", aggregate_id, at,
            self._next_version(aggregate_id),
            {
                "customer_id": customer_id,
                "medication_key": medication_key,
                "source_kind": source_kind,
                "declared_by": declared_by,
                "declared_at": at.isoformat(),
            },
        )
        self.append(event)

    def activate_plan(self, customer_id: str, plan_id: str, pharmacist_id: str, store_id: str,
                      obligations: Sequence[Mapping[str, Any]], basis_event_ids: Sequence[str],
                      at: datetime, valid_until: datetime | None = None,
                      sales_offer_id: str | None = None, supersedes: int | None = None) -> None:
        """激活或换版服务计划。

        销售优惠（``sales_offer_id``）只被记录为关联，不允许改变义务内容；
        计划始终声明不自动诊断。
        """
        _require_pharmacist(self.state, pharmacist_id, store_id, at)
        existing = self.state.plans.get(plan_id)
        if supersedes is None:
            if existing is not None:
                raise ChainError(f"计划 {plan_id} 已存在，换版须显式声明 supersedes")
            version = 1
            event_type = "PLAN_ACTIVATED"
        else:
            if existing is None or existing["plan_version"] != supersedes:
                current = existing["plan_version"] if existing else None
                raise ChainError(f"只能基于上一版本换版（声明 {supersedes}，当前 {current}）")
            version = supersedes + 1
            event_type = "PLAN_SUPPLANTED"
        _require_scope(self.state, SCOPE_REMINDER)

        normalised = [self._normalise_obligation(o) for o in obligations]
        keys = [o["key"] for o in normalised]
        if len(set(keys)) != len(keys):
            raise ChainError("同一计划内义务键必须唯一")
        for key in keys:
            if key in self.state.obligations and self.state.obligations[key].status == "open":
                raise ChainError(f"义务 {key} 已存在且尚未履行，换版不得重建")

        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "plan_version": version,
            "customer_id": customer_id,
            "pharmacist_id": pharmacist_id,
            "store_id": store_id,
            "basis_event_ids": list(basis_event_ids),
            "obligations": normalised,
            "sales_offer_id": sales_offer_id,
            "diagnosis_declined": True,
            "valid_from": at.isoformat(),
            "valid_until": valid_until.isoformat() if valid_until else None,
        }
        if supersedes is not None:
            payload["supersedes_version"] = supersedes
        event = _envelope(event_type, "service_plan", f"plan-{plan_id}", at,
                          self._next_version(f"plan-{plan_id}"), payload)
        self.append(event)

    @staticmethod
    def _normalise_obligation(raw: Mapping[str, Any]) -> dict[str, Any]:
        if raw["fact_kind"] not in FACT_KINDS:
            raise ChainError(f"未知义务类型：{raw['fact_kind']}")
        start = raw["window_start"]
        end = raw["window_end"]
        if end <= start:
            raise ChainError("回访窗口结束时间必须晚于开始时间")
        return {
            "key": raw["key"],
            "fact_kind": raw["fact_kind"],
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
        }

    # 提醒、回访与核验 -------------------------------------------------------

    def call_reminder(self, customer_id: str, obligation_key: str, called_at: datetime) -> str:
        """按计划呼叫一次提醒，返回事实键。提醒依据来自当前计划版本与来源声明。"""
        obligation = _obligation_open(self.state, obligation_key)
        _require_scope(self.state, FACT_SCOPE[obligation.fact_kind])
        if called_at < obligation.window_start or called_at > obligation.window_end:
            raise ChainError("提醒只能在计划保存的回访窗口内呼叫")
        plan = _plan_of(self.state, obligation.plan_id, obligation.plan_version)
        fact_key = f"fact-{obligation.key}"
        evidence = [e for e in (self._latest_source_event(customer_id),) if e]
        event = _envelope(
            "REMINDER_DUE_CALLED", "service_fact", fact_key, called_at,
            self._next_version(fact_key),
            {
                "customer_id": customer_id,
                "fact_kind": obligation.fact_kind,
                "plan_id": obligation.plan_id,
                "plan_version": obligation.plan_version,
                "store_id": plan["store_id"],
                "obligation_key": obligation.key,
                "fact_key": fact_key,
                "window_start": obligation.window_start.isoformat(),
                "window_end": obligation.window_end.isoformat(),
                "called_at": called_at.isoformat(),
                "evidence_event_ids": evidence,
                "diagnosis_declined": True,
            },
        )
        self.append(event)
        return fact_key

    def confirm_service(self, fact_key: str, customer_id: str, obligation_key: str,
                        pharmacist_id: str, store_id: str, outcome: str, notes: str,
                        at: datetime, sales_offer_id: str | None = None) -> None:
        """药师签认亲自完成的专业服务。销售优惠不改变结论与计划。

        不同门店对同一轮回访的签认，凭相同 ``fact_key`` 归入同一次服务事实。
        """
        obligation = self.state.obligations.get(obligation_key)
        if obligation is None:
            raise ChainError(f"义务 {obligation_key} 不存在")
        if obligation.status not in ("open", "confirmed"):
            raise ChainError(f"义务 {obligation_key} 已 {obligation.status}，不能再签认回访")
        if fact_key != f"fact-{obligation.key}":
            raise ChainError("事实键必须与义务对应，跨店重复回访应复用同一事实键")
        if outcome not in OUTCOME_CONTRADICTS:
            raise ChainError("回访结论只能是 taken / missed")
        _require_scope(self.state, FACT_SCOPE[obligation.fact_kind])
        _require_pharmacist(self.state, pharmacist_id, store_id, at)
        _plan_of(self.state, obligation.plan_id, obligation.plan_version)

        existing = self.state.facts.get(fact_key)
        if existing is None:
            raise ChainError("必须先按计划呼叫提醒形成事实，才能签认回访")
        if any(c.pharmacist_id == pharmacist_id for c in existing.confirmations):
            raise ChainError("同一药师对同一事实只能签认一次")

        event = _envelope(
            "SERVICE_CONFIRMED", "service_fact", fact_key, at,
            self._next_version(fact_key),
            {
                "fact_key": fact_key,
                "fact_kind": obligation.fact_kind,
                "customer_id": customer_id,
                "plan_id": obligation.plan_id,
                "plan_version": obligation.plan_version,
                "obligation_key": obligation.key,
                "store_id": store_id,
                "pharmacist_id": pharmacist_id,
                "outcome": outcome,
                "notes": notes,
                "confirmed_at": at.isoformat(),
                "personally_performed": True,
                "sales_offer_id": sales_offer_id,
            },
        )
        self.append(event)

    def resolve_discrepancy(self, fact_key: str, verifier_pharmacist_id: str,
                            chosen_confirmation_id: str, at: datetime) -> None:
        """矛盾结论交由未参与该事实销售的药师核验。"""
        fact = self.state.facts.get(fact_key)
        if fact is None or len(fact.confirmations) < 2:
            raise ChainError("只有跨店多次回访之间出现矛盾时才需要核验")
        outcomes = {c.outcome for c in fact.confirmations}
        if len(outcomes) < 2:
            raise ChainError("回访内容一致，无需核验")
        candidate = next((c for c in fact.confirmations if c.event_id == chosen_confirmation_id), None)
        if candidate is None:
            raise ChainError("被选定的确认不存在")
        # 核验者必须资质有效，且不是任何关联销售优惠的参与方（未参与销售）。
        if not _is_pharmacist_qualified(self.state, verifier_pharmacist_id, at):
            raise ChainError("核验药师资质缺失或已失效")
        sellers = {c.pharmacist_id for c in fact.confirmations if c.sales_offer_id}
        if verifier_pharmacist_id in sellers:
            raise ChainError("参与销售的药师不得核验该事实，须由未参与销售的药师核验")
        if not _is_pharmacist_qualified(self.state, candidate.pharmacist_id, candidate.confirmed_at):
            raise ChainError("被选定结论的签认药师当时资质无效")

        event = _envelope(
            "DISCREPANCY_RESOLVED", "service_fact", fact_key, at,
            self._next_version(fact_key),
            {
                "fact_key": fact_key,
                "customer_id": fact.customer_id,
                "verifier_pharmacist_id": verifier_pharmacist_id,
                "verifier_store_id": self._pharmacist_store(verifier_pharmacist_id, at),
                "sales_independent": True,
                "chosen_confirmation_id": chosen_confirmation_id,
                "resolved_at": at.isoformat(),
            },
        )
        self.append(event)

    def escalate(self, fact_key: str, customer_id: str, obligation_key: str,
                 pharmacist_id: str, store_id: str, reason_code: str,
                 at: datetime, evidence_event_ids: Sequence[str]) -> None:
        """缺药等异常按保存的窗口升级。"""
        obligation = _obligation_open(self.state, obligation_key)
        _require_pharmacist(self.state, pharmacist_id, store_id, at)
        if reason_code not in ("drug_shortage", "customer_unreachable", "safety_risk"):
            raise ChainError("未登记的异常原因")
        event = _envelope(
            "EXCEPTION_ESCALATED", "escalation_case", f"escalation-{fact_key}", at,
            self._next_version(f"escalation-{fact_key}"),
            {
                "fact_key": fact_key,
                "customer_id": customer_id,
                "plan_id": obligation.plan_id,
                "plan_version": obligation.plan_version,
                "obligation_key": obligation.key,
                "store_id": store_id,
                "pharmacist_id": pharmacist_id,
                "reason_code": reason_code,
                "window_start": obligation.window_start.isoformat(),
                "window_end": obligation.window_end.isoformat(),
                "escalated_at": at.isoformat(),
                "evidence_event_ids": list(evidence_event_ids),
            },
        )
        self.append(event)

    # 门店承接 ---------------------------------------------------------------

    def withdraw_store(self, store_id: str, reason: str, effective_at: datetime) -> None:
        if reason not in (REASON_STORE_CLOSURE, REASON_QUALIFICATION_LAPSE):
            raise ChainError("未登记的退出原因")
        event = _envelope(
            "STORE_WITHDRAWN", "continuity_handoff", f"store-{store_id}", effective_at,
            self._next_version(f"store-{store_id}"),
            {"store_id": store_id, "effective_at": effective_at.isoformat(), "reason": reason},
        )
        self.append(event)

    def transferable_obligations(self, customer_id: str, from_store: str) -> list[Obligation]:
        """门店关闭或资质失效时，只迁移尚未履行的义务（含已升级待处理者）。"""
        return [
            o for o in self.state.obligations.values()
            if o.store_id == from_store and o.status in ("open", "escalated")
        ]

    def accept_handoff(self, handoff_id: str, customer_id: str, from_store: str, to_store: str,
                       obligation_keys: Sequence[str], accepting_pharmacist_id: str,
                       at: datetime) -> None:
        """两家门店同时接手同一未来责任时，只有一家能成功。

        约束：义务必须属于退出方且仍 open/escalated；一旦已被任一生效承接拿走
        （其责任门店已变更），后到的承接即失败。持久化顺序由事件日志决定，天然唯一赢家。
        """
        _require_scope(self.state, SCOPE_HANDOFF)
        _require_pharmacist(self.state, accepting_pharmacist_id, to_store, at)
        if from_store not in self.state.stores_withdrawn:
            raise ChainError("只有门店已退出（关闭/资质失效）才能发起承接")
        if to_store in self.state.stores_withdrawn:
            raise ChainError("已退出门店不能承接责任")
        if handoff_id in self.state.handoffs:
            raise ChainError("承接单已存在")
        keys = list(obligation_keys)
        if not keys:
            raise ChainError("承接必须包含至少一项义务")
        if len(set(keys)) != len(keys):
            raise ChainError("承接义务不得重复")
        for key in keys:
            obligation = self.state.obligations.get(key)
            if obligation is None:
                raise ChainError(f"义务 {key} 不存在")
            if obligation.store_id != from_store:
                raise ChainError(f"义务 {key} 不属于退出门店 {from_store}")
            if obligation.status not in ("open", "escalated"):
                raise ChainError(f"义务 {key} 非未履行状态（{obligation.status}），不能承接")
        plan_ids = {self.state.obligations[k].plan_id for k in keys}
        plan_versions = {self.state.obligations[k].plan_version for k in keys}
        if len(plan_ids) != 1:
            raise ChainError("一次承接只能基于同一服务计划")
        event = _envelope(
            "HANDOFF_ACCEPTED", "continuity_handoff", handoff_id, at,
            self._next_version(handoff_id),
            {
                "handoff_id": handoff_id,
                "customer_id": customer_id,
                "from_store": from_store,
                "to_store": to_store,
                "accepting_pharmacist_id": accepting_pharmacist_id,
                "obligation_keys": keys,
                "plan_id": next(iter(plan_ids)),
                "plan_version": next(iter(plan_versions)),
                "accepted_at": at.isoformat(),
            },
        )
        self.append(event)

    def reject_handoff(self, handoff_id: str, from_store: str, to_store: str,
                       reason: str, at: datetime) -> None:
        event = _envelope(
            "HANDOFF_REJECTED", "continuity_handoff", handoff_id, at,
            self._next_version(handoff_id),
            {
                "handoff_id": handoff_id,
                "from_store": from_store,
                "to_store": to_store,
                "reason": reason,
                "rejected_at": at.isoformat(),
            },
        )
        self.append(event)

    def complete_handoff(self, handoff_id: str, at: datetime) -> None:
        handoff = self.state.handoffs.get(handoff_id)
        if handoff is None or handoff.status != "accepted":
            raise ChainError("只有已生效的承接能完成")
        event = _envelope(
            "HANDOFF_COMPLETED", "continuity_handoff", handoff_id, at,
            self._next_version(handoff_id),
            {
                "handoff_id": handoff_id,
                "from_store": handoff.from_store,
                "to_store": handoff.to_store,
                "transferred_obligation_keys": sorted(handoff.obligation_keys),
                "completed_at": at.isoformat(),
            },
        )
        self.append(event)

    # 应急库存、退出与法定证明 -----------------------------------------------

    def commit_emergency_stock(self, commitment_id: str, customer_id: str, store_id: str,
                               medication_key: str, quantity: int, committed_by: str,
                               at: datetime, valid_until: datetime) -> None:
        if quantity <= 0:
            raise ChainError("应急库存数量必须为正")
        if valid_until <= at:
            raise ChainError("库存承诺的保留时限必须晚于当前时间")
        event = _envelope(
            "EMERGENCY_STOCK_COMMITTED", "emergency_stock_commitment", commitment_id, at,
            self._next_version(commitment_id),
            {
                "commitment_id": commitment_id,
                "customer_id": customer_id,
                "store_id": store_id,
                "medication_key": medication_key,
                "quantity": quantity,
                "valid_until": valid_until.isoformat(),
                "committed_by": committed_by,
                "committed_at": at.isoformat(),
            },
        )
        self.append(event)

    def decide_exit(self, customer_id: str, scope: str, decided_by: str,
                    at: datetime, reason: str) -> None:
        """顾客退出陪伴服务；尚未履行的义务随之取消，法定证明仍可开具。"""
        if scope not in ("full_service",) + tuple(EXTRA_SCOPES):
            raise ChainError("未登记的退出范围")
        if self.state.exit is not None:
            raise ChainError("已记录退出决定")
        event = _envelope(
            "EXIT_DECIDED", "exit_record", f"exit-{customer_id}", at,
            self._next_version(f"exit-{customer_id}"),
            {
                "customer_id": customer_id,
                "scope": scope,
                "decided_by": decided_by,
                "decided_at": at.isoformat(),
                "reason": reason,
            },
        )
        self.append(event)

    def issue_legal_proof(self, proof_id: str, customer_id: str, store_id: str,
                          proof_kind: str, period_start: datetime, period_end: datetime,
                          issued_by: str, at: datetime) -> None:
        """法定购药/服务证明：不依赖营销等额外授权，撤回或退出后仍可开具。"""
        if proof_kind not in ("purchase_record", "service_record"):
            raise ChainError("证明类型只能是 purchase_record / service_record")
        if period_end < period_start:
            raise ChainError("证明区间结束时间不得早于开始时间")
        event = _envelope(
            "LEGAL_PROOF_ISSUED", "legal_proof", proof_id, at,
            self._next_version(proof_id),
            {
                "proof_id": proof_id,
                "customer_id": customer_id,
                "store_id": store_id,
                "proof_kind": proof_kind,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "issued_at": at.isoformat(),
                "issued_by": issued_by,
            },
        )
        self.append(event)

    # 受限接口 ---------------------------------------------------------------

    def responsibility_view(self, customer_id: str, role: str) -> ResponsibilityView:
        """受限接口：说明当前责任人、计划版本与每次提醒的事实依据，并声明不自动诊断。"""
        if role not in ROLE_VISIBLE_FIELDS:
            raise ChainError("未知角色")
        # 取该顾客最新计划（计划记录按 customer 维度）。这里用日志中该顾客最后一条计划。
        plan_record = None
        for event in self.state.log:
            if event["event_type"] in ("PLAN_ACTIVATED", "PLAN_SUPPLANTED") and \
                    event["payload"].get("customer_id") == customer_id:
                plan_record = event["payload"]

        reminders: list[Mapping[str, Any]] = []
        responsible_store = None
        responsible_pharmacist = None
        plan_view = None
        if plan_record is not None and "plan" in ROLE_VISIBLE_FIELDS[role]:
            obligation_views = [
                self.state.obligations[o["key"]]
                for o in plan_record["obligations"]
                if o["key"] in self.state.obligations
            ]
            plan_view = PlanView(
                plan_id=plan_record["plan_id"],
                plan_version=plan_record["plan_version"],
                store_id=plan_record["store_id"],
                pharmacist_id=plan_record["pharmacist_id"],
                obligations=tuple(obligation_views),
                valid_from=_parse_dt(plan_record["valid_from"]),
                valid_until=_parse_dt(plan_record["valid_until"]) if plan_record["valid_until"] else None,
            )
            open_obs = [o for o in obligation_views if o.status in ("open", "escalated")]
            if open_obs:
                responsible_store = open_obs[0].store_id
                responsible_pharmacist = open_obs[0].pharmacist_id
            for event in self.state.log:
                if event["event_type"] != "REMINDER_DUE_CALLED":
                    continue
                p = event["payload"]
                if p.get("customer_id") != customer_id:
                    continue
                fact = self.state.facts.get(p["fact_key"])
                reminders.append(
                    {
                        "fact_key": p["fact_key"],
                        "obligation_key": p["obligation_key"],
                        "plan_version": p["plan_version"],
                        "window_start": p["window_start"],
                        "window_end": p["window_end"],
                        "called_at": p["called_at"],
                        "evidence_event_ids": list(p["evidence_event_ids"]),
                        "current_outcome": fact.resolved_outcome
                        if fact and fact.resolved_outcome
                        else (fact.confirmations[-1].outcome if fact and fact.confirmations else None),
                    }
                )
        return ResponsibilityView(
            customer_id=customer_id,
            responsible_store=responsible_store,
            responsible_pharmacist=responsible_pharmacist,
            plan=plan_view,
            reminders=tuple(reminders),
            diagnosis_notice=DIAGNOSIS_NOTICE,
        )

    # 辅助 -------------------------------------------------------------------

    def _latest_source_event(self, customer_id: str) -> str | None:
        for event in reversed(self.state.log):
            if event["event_type"] == "SOURCE_DECLARED" and \
                    event["payload"].get("customer_id") == customer_id:
                return event["event_id"]
        return None

    def _pharmacist_store(self, pharmacist_id: str, at: datetime) -> str:
        shift = self.state.shifts.get(pharmacist_id)
        if shift and _parse_dt(shift["valid_from"]) <= at < _parse_dt(shift["valid_until"]):
            return shift["store_id"]
        raise ChainError(f"药师 {pharmacist_id} 当前无有效班次门店")

    def _next_version(self, aggregate_id: str) -> int:
        """预读该聚合下一个版本号；权威递增发生在 apply_event。"""
        return self.state.versions.get(aggregate_id, 0) + 1


def rebuild(schema: Mapping[str, Any], saved_events: Sequence[Mapping[str, Any]],
            sink: Callable[[Mapping[str, Any]], None] | None = None) -> ResponsibilityChain:
    """从已保存事件重建责任链，交接中断后据此按保存状态继续。"""
    return ResponsibilityChain(schema, history=saved_events, sink=sink)
