from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pharmacy_care_continuity.stream import validate_stream
from pharmacy_care_continuity.views import build_responsibility_view, validate_view
from tests.helpers import event, followup, plan_v1, qualified, reminder, consent


def _schema():
    return json.loads((ROOT / "contracts" / "view.schema.json").read_text(encoding="utf-8"))


def _basic_stream():
    return [consent(), qualified(), plan_v1(),
            reminder("ob1", at="2026-09-20T10:00:00+08:00", window_end="2026-09-20T12:00:00+08:00")]


class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = _schema()

    def test_pharmacist_view_states_responsible_plan_version_and_basis(self) -> None:
        state, issues = validate_stream(_basic_stream())
        self.assertEqual([], issues)
        view = build_responsibility_view(state, "S1", as_of="2026-09-20T11:00:00+08:00", viewer_role="pharmacist")
        self.assertEqual([], validate_view(view, self.schema))
        self.assertEqual("A", view["responsible"]["store_id"])
        self.assertEqual("e-plan1", view["responsible"]["basis_event_id"])
        self.assertEqual(1, view["plan"]["plan_version"])
        self.assertEqual(1, len(view["active_reminders"]))
        self.assertEqual("e-plan1", view["active_reminders"][0]["factual_basis"]["based_on_event_id"])

    def test_clerk_cannot_see_factual_basis_or_medical_history(self) -> None:
        state, _ = validate_stream(_basic_stream())
        view = build_responsibility_view(state, "S1", as_of="2026-09-20T11:00:00+08:00", viewer_role="clerk")
        self.assertEqual([], validate_view(view, self.schema))
        self.assertNotIn("factual_basis", view["active_reminders"][0])
        self.assertNotIn("medical_history", view["visible_sections"])
        self.assertNotIn("pharmacist_id", view["responsible"])
        # 但仍能看到继续跟进所需的窗口与依据事件标识
        self.assertIn("window_end", view["active_reminders"][0])
        self.assertIn("basis_event_id", view["active_reminders"][0])

    def test_view_must_carry_no_diagnosis_disclaimer(self) -> None:
        state, _ = validate_stream(_basic_stream())
        view = build_responsibility_view(state, "S1", as_of="2026-09-20T11:00:00+08:00", viewer_role="pharmacist")
        view["diagnosis_disclaimer"] = "系统将自动诊断病情"
        issues = validate_view(view, self.schema)
        self.assertIn("disclaimer_required", {i.code for i in issues})

    def test_missing_responsible_store_is_rejected(self) -> None:
        bad = {
            "subject_id": "S1",
            "as_of": "2026-09-20T11:00:00+08:00",
            "viewer_role": "pharmacist",
            "responsible": {"store_id": "", "basis_event_id": ""},
            "plan": {"plan_id": "plan1", "plan_version": 1, "basis_event_id": "e-plan1"},
            "active_reminders": [],
            "open_obligations": [],
            "diagnosis_disclaimer": "本系统不自动作出诊断；健康判断由具备资质的药师作出。",
            "visible_sections": [],
        }
        issues = validate_view(bad, self.schema)
        codes = {i.code for i in issues}
        self.assertIn("required", codes)

    def test_clerk_view_with_medical_history_section_is_rejected(self) -> None:
        state, _ = validate_stream(_basic_stream())
        view = build_responsibility_view(state, "S1", as_of="2026-09-20T11:00:00+08:00", viewer_role="clerk")
        view["visible_sections"].append("medical_history")
        issues = validate_view(view, self.schema)
        self.assertIn("minimum_necessary", {i.code for i in issues})

    def test_sample_files_are_valid_views(self) -> None:
        for name in ("sample_view_pharmacist.json", "sample_view_clerk.json"):
            view = json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))
            self.assertEqual([], validate_view(view, self.schema), name)


if __name__ == "__main__":
    unittest.main()
