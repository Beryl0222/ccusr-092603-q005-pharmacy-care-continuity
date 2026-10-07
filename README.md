# 药店健康陪伴责任链

连接药店网点、药师资格和长期健康服务责任，支持停店或换人后的连续承接。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/pharmacy_care_continuity/contracts.py`：交换层契约校验与命令行入口。
- `src/pharmacy_care_continuity/chain.py`：责任链业务层（事件溯源、授权、资质、去重核验、承接、恢复、受限接口）。
- `tests/`：交换层字段/时间/版本/载荷测试与责任链业务规则测试。
- `docs/domain.md`：领域对象、事件语义与业务规则。

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
PYTHONPATH=src python3 -m pharmacy_care_continuity.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。
