# 领域约定

连接药店网点、药师资格和长期健康服务责任，支持停店或换人后的连续承接。系统围绕**责任**而不是订单组织：谁看过什么健康信息、哪项提醒仍有效、谁应继续跟进，都能从事件流中回答。

所有时间必须携带时区；同一 `aggregate_id` 的版本号从 1 起严格递增；同一顾客的事件时间不得倒流。校验层不替调用方改写输入。

## 聚合对象

`care_subject`、`consent_grant`、`pharmacist_profile`、`pharmacist_assignment`、`medication_source`、`service_plan`、`reminder`、`service_fact`、`obligation`、`store`、`continuity_handoff`、`emergency_stock`、`care_exit`。

## 事件与责任语义

| 事件 | 责任含义 |
| --- | --- |
| `CONSENT_GRANTED` / `CONSENT_WITHDRAWN` | 分项授权（建档、用药提醒、依从回访、跨店交接、应急供应、营销用途、数据共享），逐项可撤回 |
| `PHARMACIST_QUALIFIED` / `PHARMACIST_SHIFT_SCHEDULED` / `QUALIFICATION_REVOKED` | 药师资质与班次；资质有有效窗口，撤销立即生效 |
| `MEDICATION_SOURCE_DECLARED` | 处方（店内/外院）或自购来源声明 |
| `PLAN_ACTIVATED` / `PLAN_REVISED` | 服务计划版本，版本逐 1 递增；销售优惠不得进入计划（促销字段直接拒收） |
| `REMINDER_FIRED` | 用药提醒，同时开启一条可被交接/升级的义务，携带事实依据事件 |
| `FOLLOWUP_RECORDED` | 依从回访，按 `fact_key` 归并为一次服务事实 |
| `SERVICE_CONFIRMED` | 药师签认，仅可签认本人亲自完成的专业服务 |
| `FACT_CONFLICT_REPORTED` / `FACT_VERIFIED` | 矛盾事实由未参与销售、且非该事实记录人的药师核验 |
| `ANOMALY_ESCALATED` | 缺药、错过回访窗口等异常升级 |
| `STORE_WITHDRAWN` | 门店停业/收缩，生效后不得新开义务 |
| `HANDOFF_PROPOSED` / `HANDOFF_COMPLETED` | 门店承接；只迁移未履行义务，竞争接管仅一家成功 |
| `EMERGENCY_STOCK_COMMITTED` | 承接门店或邻近门店的应急库存承诺 |
| `CARE_EXITED` | 退出决定；冻结未来服务，但保留法定购药与服务证明 |

## 跨事件业务规则（`stream_constraints`）

- **事实去重**：同一顾客下相同 `fact_key` 的回访归为一次 `service_fact`；`content_hash` 矛盾时事实进入冲突态，未经独立药师 `FACT_VERIFIED` 不得签认。
- **独家承接**：交接完成时校验每条义务当前归属门店必须等于 `from_store`；两家门店同时接手时后到的一家报 `responsibility_taken`，义务归属不变。
- **只迁移未履行义务**：已 `fulfilled` 的义务出现在交接清单中会被拒绝。
- **药师签认**：`personally_performed` 必须为真；签认时刻药师在该门店资质须处于有效窗口且未被撤销。
- **授权闸门**：提醒依赖 `medication_reminder`、回访依赖 `adherence_followup`、跨店交接依赖 `cross_store_handoff`、应急供应依赖 `emergency_supply`。撤回 `marketing_use`/`data_sharing` 不影响法定购药与服务证明，也不阻断健康服务。
- **销售隔离**：计划事件载荷中出现 `promotion_id`、`discount`、`coupon`、`sales_offer` 一律拒收。
- **退出冻结**：`CARE_EXITED.effective_at` 之后不得再产生提醒、回访或新义务；`legal_proof_retained` 必须为真。

## 断点续接

事件折叠为纯函数（`fold_events`），`StreamState.snapshot()` 输出可 JSON 持久化的保存状态，`StreamState.restore()` 恢复后继续折叠即可。进程在交接中断后，原有回访窗口、缺药升级和责任移交均按保存状态继续；重放重复 `event_id` 只报一次幂等冲突，不重复生效。

## 受限接口视图

见 `contracts/view.schema.json`。视图必须同时说明：

1. 当前责任门店与药师，以及依据事件标识；
2. 采用的服务计划版本与依据事件；
3. 每条仍有效提醒的回访窗口和事实依据；
4. 固定声明：本系统不自动作出诊断，健康判断由具备资质的药师作出。

普通店员（`clerk`）走最小知情：看不到计划内容、提醒事实依据细节和 `medical_history` 区块，只能看到继续跟进所需的门店、窗口、义务标识及依据事件号。

## 分层

- 交换层（`contracts.py` + `domain.schema.json`）：单事件结构、枚举、时间、版本、必需载荷。
- 业务层（`stream.py`）：跨事件责任规则与折叠状态。
- 接口层（`views.py` + `view.schema.json`）：按角色投影的当前责任视图。

同一事件标识的写入幂等与存储冲突属于上层服务职责；本契约负责稳定地报告结构与业务问题。
