# 药店健康陪伴责任链

连接药店网点、药师资格和长期健康服务责任，支持停店或换人后的连续承接。围绕**责任**而非订单组织：分项授权、药师资质与班次、处方/自购来源声明、计划版本、依从回访、矛盾核验、异常升级、门店承接、应急库存承诺与退出决定全部以事件记录。

## 目录

- `contracts/domain.schema.json`：事件信封、19 类事件、13 类聚合与载荷约定。
- `contracts/view.schema.json`：受限接口责任视图约定（责任人/计划版本/提醒依据/不自动诊断/最小知情）。
- `data/sample.json`：单事件联调样例。
- `data/sample_stream.json`：甲店建档 → 乙店提醒 → 甲店停业交接乙店 → 缺药由丙店应急供应 → 撤回营销授权 → 退出 的完整事件流。
- `data/sample_view_pharmacist.json` / `data/sample_view_clerk.json`：同一保存状态下药师与店员两种角色视图。
- `src/pharmacy_care_continuity/`：契约校验（`contracts`）、事件流业务规则与断点续接（`stream`）、受限视图（`views`）、命令行入口。
- `tests/`：契约边界、责任链业务规则（独家承接、事实核验、资质窗口、退出冻结等）、快照续接与视图角色测试。
- `docs/domain.md`：领域对象、事件语义与分层说明。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
# 单个事件
PYTHONPATH=src python3 -m pharmacy_care_continuity.cli event contracts/domain.schema.json data/sample.json

# 整条责任链事件流（先结构校验，再跨事件业务规则校验）
PYTHONPATH=src python3 -m pharmacy_care_continuity.cli stream contracts/domain.schema.json data/sample_stream.json

# 受限接口视图
PYTHONPATH=src python3 -m pharmacy_care_continuity.cli view contracts/view.schema.json data/sample_view_pharmacist.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。
