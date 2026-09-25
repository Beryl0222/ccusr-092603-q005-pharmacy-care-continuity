# 领域约定

连接药店网点、药师资格和长期健康服务责任，支持停店或换人后的连续承接。

聚合对象包括`care_subject`、`service_plan`、`pharmacist_assignment`、`continuity_handoff`。事件类型包括`CONSENT_RECORDED`、`PLAN_ACTIVATED`、`SERVICE_CONFIRMED`、`STORE_WITHDRAWN`、`HANDOFF_COMPLETED`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `PLAN_ACTIVATED`：还需包含 `plan_version`, `pharmacist_id`。
- `STORE_WITHDRAWN`：还需包含 `effective_at`, `reason`。
- `HANDOFF_COMPLETED`：还需包含 `from_store`, `to_store`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。
