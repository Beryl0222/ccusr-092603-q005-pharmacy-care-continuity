from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pharmacy_care_continuity.contracts import validate_event


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
        cls.sample = json.loads((ROOT / "data" / "sample.json").read_text(encoding="utf-8"))

    def test_sample_is_valid(self) -> None:
        self.assertEqual([], validate_event(self.sample, self.schema))

    def test_missing_envelope_fields_have_stable_order(self) -> None:
        issues = validate_event({}, self.schema)
        self.assertEqual(sorted(issue.field for issue in issues), [issue.field for issue in issues])
        self.assertIn("event_id", {issue.field for issue in issues})

    def test_time_and_version_boundaries(self) -> None:
        event = dict(self.sample, occurred_at="2026-09-24T12:00:00", version=0)
        codes = {(issue.field, issue.code) for issue in validate_event(event, self.schema)}
        self.assertIn(("occurred_at", "timezone_required"), codes)
        self.assertIn(("version", "positive_integer"), codes)

    def test_event_specific_payload_is_required(self) -> None:
        event = dict(self.sample, event_type="PLAN_ACTIVATED", payload={})
        issues = validate_event(event, self.schema)
        self.assertIn(("payload.plan_version", "required"), [(issue.field, issue.code) for issue in issues])

    def test_unknown_event_is_rejected(self) -> None:
        event = dict(self.sample, event_type="UNKNOWN")
        issues = validate_event(event, self.schema)
        self.assertIn(("event_type", "unsupported_value"), [(issue.field, issue.code) for issue in issues])

    def test_payload_enum_is_checked(self) -> None:
        event = json.loads(json.dumps(self.sample))
        event["payload"] = {"scope_dummy": 1}
        bad = dict(self.sample, payload={"scopes": ["not_a_scope"]})
        # 借助 CONSENT_GRANTED 的真实载荷形态
        bad["event_type"] = "CONSENT_GRANTED"
        bad["payload"] = {
            "subject_id": "S-1",
            "store_id": "A",
            "scopes": ["not_a_scope"],
            "granted_at": "2026-09-24T12:00:00+08:00",
        }
        issues = validate_event(bad, self.schema)
        self.assertIn(("payload.scopes", "unsupported_value"), [(i.field, i.code) for i in issues])

    def test_payload_datetime_requires_timezone(self) -> None:
        bad = dict(self.sample)
        bad["event_type"] = "STORE_WITHDRAWN"
        bad["payload"] = {"store_id": "A", "effective_at": "2026-09-24T18:00:00", "reason": "store_closed"}
        issues = validate_event(bad, self.schema)
        self.assertIn(("payload.effective_at", "timezone_required"), [(i.field, i.code) for i in issues])

    def test_sales_offer_cannot_enter_plan(self) -> None:
        payload = {
            "subject_id": "S-1",
            "plan_id": "plan-1",
            "plan_version": 1,
            "pharmacist_id": "P-1",
            "store_id": "A",
            "sales_influence": "none",
            "activated_at": "2026-09-24T12:00:00+08:00",
            "promotion_id": "PROMO-9",
        }
        issues = validate_event(dict(self.sample, event_type="PLAN_ACTIVATED", payload=payload), self.schema)
        self.assertIn(
            ("payload.promotion_id", "sales_influence_forbidden"),
            [(i.field, i.code) for i in issues],
        )

    def test_scopes_must_be_non_empty_string_array(self) -> None:
        bad = dict(self.sample, event_type="CONSENT_GRANTED")
        bad["payload"] = {
            "subject_id": "S-1",
            "store_id": "A",
            "scopes": ["care_profile", ""],
            "granted_at": "2026-09-24T12:00:00+08:00",
        }
        issues = validate_event(bad, self.schema)
        self.assertIn(("payload.scopes", "non_empty_string_array"), [(i.field, i.code) for i in issues])


if __name__ == "__main__":
    unittest.main()
