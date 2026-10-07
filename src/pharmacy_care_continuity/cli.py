"""命令行校验入口：单事件、事件流或受限接口视图。

用法:
  python -m pharmacy_care_continuity.cli event <domain.schema.json> <event.json>
  python -m pharmacy_care_continuity.cli stream <domain.schema.json> <events.json>
  python -m pharmacy_care_continuity.cli view <view.schema.json> <view.json>

成功输出 `valid`；失败逐行输出 字段<TAB>代码<TAB>中文说明，并以非零状态结束。
事件流模式先做单事件结构校验，再做跨事件业务规则校验。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .contracts import validate_event
from .stream import validate_stream
from .views import validate_view


def _load(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _report(issues) -> int:
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}	{issue.code}	{issue.message}")
    return 1


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 3 or argv[0] not in {"event", "stream", "view"}:
        print(
            "用法: python -m pharmacy_care_continuity.cli <event|stream|view> <schema.json> <input.json>",
            file=sys.stderr,
        )
        return 2

    mode, schema_path, input_path = argv
    schema = _load(schema_path)
    data = _load(input_path)

    if mode == "event":
        return _report(validate_event(data, schema))
    if mode == "view":
        return _report(validate_view(data, schema))

    events = data if isinstance(data, list) else data.get("events", [])
    issues = []
    for index, event in enumerate(events):
        for issue in validate_event(event, schema):
            issues.append(type(issue)(
                field=f"events[{index}].{issue.field}",
                code=issue.code,
                message=f"事件 {event.get('event_id', f'#{index}')}: {issue.message}",
            ))
    if not issues:
        _, issues = validate_stream(events)
    return _report(issues)


if __name__ == "__main__":
    raise SystemExit(main())
