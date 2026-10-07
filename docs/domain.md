# 领域约定

连接药店网点、药师资格和长期健康服务责任，支持停店或换人后的连续承接。系统只记录与呈现事实，**不自动作出诊断**。

## 分层

- 交换层（`contracts.py` / `domain.schema.json`）：稳定报告结构、枚举、时间、版本与各事件必填载荷，不替调用方改写输入。
- 业务层（`chain.py`）：事件溯源的责任链服务，负责授权、资质、去重、矛盾核验、承接唯一成功、隔离与崩溃恢复。所有业务结论都由已保存事件重放得到。

所有时间都必须携带时区；同一聚合上的 `version` 从 1 开始连续递增，乱序或跳号提交会被拒绝。事件标识 `event_id` 全局只接受一次。

## 聚合对象

`care_subject`、`consent_grant`、`service_plan`、`pharmacist_directory`、`pharmacist_assignment`、`medication_source`、`service_fact`、`escalation_case`、`continuity_handoff`、`emergency_stock_commitment`、`exit_record`、`legal_proof`。

## 事件与载荷

| 事件 | 含义 | 关键载荷 |
| --- | --- | --- |
| `CONSENT_RECORDED` | 分项授权记录 | `scopes`, `granted` |
| `CONSENT_SCOPE_WITHDRAWN` | 撤回某一额外数据用途 | `scope` |
| `PLAN_ACTIVATED` / `PLAN_SUPPLANTED` | 计划激活 / 换版 | `plan_version`, `obligations`, `basis_event_ids`, `sales_offer_id`, `diagnosis_declined`；换版含 `supersedes_version` |
| `PHARMACIST_QUALIFICATION_REGISTERED` | 药师资质登记 | `license_no`, `valid_from`, `valid_until` |
| `PHARMACIST_SHIFT_OPENED` / `PHARMACIST_SHIFT_CLOSED` | 班次 | `store_id`, 有效期 |
| `SOURCE_DECLARED` | 处方 / 自购来源声明 | `medication_key`, `source_kind` |
| `REMINDER_DUE_CALLED` | 在保存窗口内呼叫提醒 | `obligation_key`, `fact_key`, `window_start/end`, `evidence_event_ids` |
| `SERVICE_CONFIRMED` | 药师签认亲自完成的回访 | `fact_key`, `outcome`, `personally_performed`, `sales_offer_id` |
| `DISCREPANCY_RESOLVED` | 矛盾结论的独立核验 | `verifier_pharmacist_id`, `sales_independent`, `chosen_confirmation_id` |
| `EXCEPTION_ESCALATED` | 缺药等异常升级 | `reason_code`, 窗口, `evidence_event_ids` |
| `STORE_WITHDRAWN` | 门店关闭 / 资质失效 | `effective_at`, `reason` |
| `HANDOFF_ACCEPTED` / `HANDOFF_REJECTED` / `HANDOFF_COMPLETED` | 门店承接 | `from_store`, `to_store`, `obligation_keys`, `plan_id/version` |
| `EMERGENCY_STOCK_COMMITTED` | 应急库存承诺 | `medication_key`, `quantity`, `valid_until` |
| `EXIT_DECIDED` | 顾客退出决定 | `scope`, `reason` |
| `LEGAL_PROOF_ISSUED` | 法定购药 / 服务证明 | `proof_kind`, 区间 |

完整必填项以 `contracts/domain.schema.json` 为准。

## 业务规则

### 分项授权

授权按 scope 记录（用药提醒、依从回访、跨店承接、营销分析等）。撤回某一额外用途后，仅依赖该分项且**尚未履行**的义务随之取消；法定购药与服务证明（`LEGAL_PROOF_ISSUED`）不属于额外用途，撤回或退出后仍可开具。

### 药师签认

只有资质在有效期内、且在责任门店当班的药师，才能签认专业服务；`personally_performed` 表示亲自完成，同一药师对同一事实只能签认一次。

### 计划版本与销售隔离

计划按整数版本推进，换版必须显式声明上一版本；上一版本尚未履行的义务自动作废。`sales_offer_id` 只记录销售关联，**不改变计划义务与回访结论**；计划与提醒都带 `diagnosis_declined=true`。

### 一次服务事实与矛盾核验

跨门店对同一轮提醒的回访，凭相同 `fact_key` 归并为一次 `service_fact`。结论一致则合并；结论矛盾（taken/missed）时，由**未参与该事实销售**、资质有效的药师核验并选定一条确认。

### 异常升级与承接

缺药等异常在保存的回访窗口内升级，义务进入 `escalated`。门店 `STORE_WITHDRAWN` 后，只迁移**尚未履行**的义务（open/escalated）。承接只变更义务的责任门店与接手药师，义务仍未履行；一旦责任迁走，后到门店对同一义务的承接必然失败，因此两家门店同时接手时只有一家成功。

### 退出

`EXIT_DECIDED` 取消所有尚未履行的义务；已履行事实与法定证明保留。

### 最小访问

受限接口 `responsibility_view` 按角色裁剪：药师可见计划、义务、提醒依据与责任人；普通店员只见顾客标识，看不到无关病史。返回内容包含当前责任门店/药师、采用的计划版本、每次提醒的 `evidence_event_ids`，并固定声明系统不自动诊断。

### 崩溃恢复

命令在成功前先把事件交给 `sink` 持久化，再进入状态；`sink` 失败则状态不变。用 `rebuild(schema, saved_events)` 可从已保存事件完整复原，交接中断后原有回访窗口、缺药升级与责任移交严格按保存状态继续。

## 边界

同一事件标识的去重、跨进程并发的加锁顺序等属于上层业务服务职责；交换层只负责结构校验。业务层的 `ChainError` 表示规则冲突，该情况下不会产生任何事件。
