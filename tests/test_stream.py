from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pharmacy_care_continuity.stream import StreamState, fold_events, validate_stream
from tests.helpers import (
    confirm,
    consent,
    event,
    followup,
    plan_v1,
    qualified,
    reminder,
)


def base(*, scopes=None):
    return [consent(scopes=scopes), qualified(), plan_v1()]


class StreamRuleTests(unittest.TestCase):
    def test_happy_path_is_clean(self) -> None:
        events = base() + [
            reminder("ob1"),
            followup("f1"),
            confirm("ob1", "f1"),
        ]
        _, issues = validate_stream(events)
        self.assertEqual([], issues)

    def test_version_gap_is_reported(self) -> None:
        events = [consent(version=1)]
        bad = consent(version=3, event_id="e-consent-2")
        bad["aggregate_id"] = "g1"
        _, issues = validate_stream(events + [bad])
        self.assertTrue(any(i.code == "version_gap" for i in issues))

    def test_duplicate_event_id_is_idempotency_conflict(self) -> None:
        events = base() + [reminder("ob1", event_id="dup", reminder_id="r1")]
        events.append(reminder("ob2", event_id="dup", reminder_id="r2", at="2026-09-20T10:01:00+08:00"))
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "duplicate_event_id" for i in issues))

    def test_same_fact_key_dedups_but_conflicting_content_blocks_signoff(self) -> None:
        # 两家门店同一 fact_key、相同内容 → 归并为一次事实，可签认
        events = base() + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            followup("f1", store="A", content_hash="sha256:same", version=1, at="2026-09-20T10:20:00+08:00"),
            followup("f1", store="B", content_hash="sha256:same", version=2, recorded_by="P1", at="2026-09-20T10:40:00+08:00"),
            confirm("ob1", "f1", at="2026-09-20T10:50:00+08:00"),
        ]
        _, issues = validate_stream(events)
        self.assertEqual([], issues)

        # 内容矛盾 → 未核验前禁止签认
        conflicting = base() + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            followup("f1", store="A", content_hash="sha256:aaa", version=1, at="2026-09-20T10:20:00+08:00"),
            followup("f1", store="B", content_hash="sha256:bbb", version=2, recorded_by="clerk-b", at="2026-09-20T10:40:00+08:00"),
            confirm("ob1", "f1", at="2026-09-20T10:50:00+08:00"),
        ]
        _, issues = validate_stream(conflicting)
        self.assertTrue(any(i.code == "unverified_conflict" for i in issues))

    def test_conflict_must_be_verified_by_sales_independent_pharmacist(self) -> None:
        def verify(*, verifier="PV", independent=True, recorder="clerk-b"):
            return event(
                "e-verify", "FACT_VERIFIED", "service_fact", "f1", "2026-09-20T10:45:00+08:00", 3,
                {
                    "subject_id": "S1",
                    "fact_key": "f1",
                    "verifier_id": verifier,
                    "verifier_sales_independent": independent,
                    "resolution": "corrected",
                    "verified_at": "2026-09-20T10:45:00+08:00",
                },
            )

        # 核验药师是记录人之一 → 拒绝
        events = base() + [
            qualified("PV", "verification-center", event_id="e-qual-v"),
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            followup("f1", store="A", content_hash="sha256:aaa", version=1, at="2026-09-20T10:20:00+08:00"),
            followup("f1", store="B", content_hash="sha256:bbb", version=2, recorded_by="PV", at="2026-09-20T10:40:00+08:00"),
            verify(verifier="PV"),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "verifier_conflict" for i in issues))

        # 未参与销售/记录的独立药师核验后可签认
        ok = base() + [
            qualified("PV", "verification-center", event_id="e-qual-v"),
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            followup("f1", store="A", content_hash="sha256:aaa", version=1, at="2026-09-20T10:20:00+08:00"),
            followup("f1", store="B", content_hash="sha256:bbb", version=2, recorded_by="clerk-b", at="2026-09-20T10:40:00+08:00"),
            verify(),
            confirm("ob1", "f1", at="2026-09-20T10:55:00+08:00"),
        ]
        _, issues = validate_stream(ok)
        self.assertEqual([], issues)

    def test_reminder_requires_reminder_consent(self) -> None:
        events = base(scopes=["care_profile"]) + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "consent_required" for i in issues))

    def test_followup_requires_followup_consent(self) -> None:
        events = base(scopes=["care_profile", "medication_reminder"]) + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            followup("f1", at="2026-09-20T10:20:00+08:00"),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "consent_required" for i in issues))

    def test_pharmacist_only_signs_personally_performed_service(self) -> None:
        events = base() + [
            reminder("ob1"),
            followup("f1"),
            confirm("ob1", "f1", personally=False),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "personal_attestation_required" for i in issues))

    def test_signoff_requires_valid_qualification_window(self) -> None:
        events = [
            consent(),
            qualified(end="2026-08-31T23:59:59+08:00"),
            plan_v1(),
            reminder("ob1"),
            followup("f1"),
            confirm("ob1", "f1"),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "qualification_required" for i in issues))

    def test_competing_handoffs_only_one_succeeds(self) -> None:
        def handoff(hid, to_store, *, completed_at, propose_at, from_store="A", version_extra=0):
            proposal = event(
                f"e-{hid}-prop", "HANDOFF_PROPOSED", "continuity_handoff", hid, propose_at, 1,
                {
                    "handoff_id": hid,
                    "subject_id": "S1",
                    "obligation_ids": ["ob1"],
                    "from_store": from_store,
                    "to_store": to_store,
                    "proposed_at": propose_at,
                },
            )
            done = event(
                f"e-{hid}-done", "HANDOFF_COMPLETED", "continuity_handoff", hid, completed_at, 2,
                {
                    "handoff_id": hid,
                    "subject_id": "S1",
                    "obligation_ids": ["ob1"],
                    "from_store": from_store,
                    "to_store": to_store,
                    "completed_at": completed_at,
                },
            )
            return [proposal, done]

        events = base() + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
        ]
        # 甲店停业
        events.append(event(
            "e-a-out", "STORE_WITHDRAWN", "store", "A", "2026-09-21T09:00:00+08:00", 1,
            {"store_id": "A", "effective_at": "2026-09-21T09:00:00+08:00", "reason": "store_closed"},
        ))
        events += handoff("hAB", "B", propose_at="2026-09-21T10:00:00+08:00", completed_at="2026-09-21T11:00:00+08:00")
        # 丙店几乎同时也想从甲店接管同一义务
        events += handoff(
            "hAC", "C", propose_at="2026-09-21T10:01:00+08:00", completed_at="2026-09-21T11:01:00+08:00"
        )
        state, issues = validate_stream(events)
        self.assertTrue(any(i.code == "responsibility_taken" for i in issues))
        # 乙店先成功，义务仍归乙店
        self.assertEqual("B", state.obligations["ob1"].owner_store)

    def test_handoff_moves_only_unfulfilled_obligations(self) -> None:
        def handoff(obligation_ids, completed_at="2026-09-21T11:00:00+08:00"):
            return [
                event("e-h-prop", "HANDOFF_PROPOSED", "continuity_handoff", "h1", "2026-09-21T10:00:00+08:00", 1,
                      {"handoff_id": "h1", "subject_id": "S1", "obligation_ids": obligation_ids,
                       "from_store": "A", "to_store": "B", "proposed_at": "2026-09-21T10:00:00+08:00"}),
                event("e-h-done", "HANDOFF_COMPLETED", "continuity_handoff", "h1", completed_at, 2,
                      {"handoff_id": "h1", "subject_id": "S1", "obligation_ids": obligation_ids,
                       "from_store": "A", "to_store": "B", "completed_at": completed_at}),
            ]

        events = base() + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            followup("f1", at="2026-09-20T10:20:00+08:00"),
            confirm("ob1", "f1", at="2026-09-20T10:50:00+08:00"),
        ]
        events += handoff(["ob1"])
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "obligation_closed" for i in issues))

    def test_exit_freezes_future_services_but_keeps_legal_proof(self) -> None:
        exit_evt = event(
            "e-exit", "CARE_EXITED", "care_exit", "x1", "2026-09-22T09:00:00+08:00", 1,
            {"subject_id": "S1", "decided_by": "S1", "exit_kind": "subject_withdrawal",
             "effective_at": "2026-09-22T09:00:00+08:00", "legal_proof_retained": True},
        )
        events = base() + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00", window_end="2026-09-20T12:00:00+08:00"),
            exit_evt,
            reminder("ob2", event_id="e-rem-ob2", reminder_id="rem-ob2",
                     at="2026-09-22T10:00:00+08:00", window_end="2026-09-22T12:00:00+08:00"),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "exit_frozen" for i in issues))

        bad_exit = dict(exit_evt)
        bad_exit["payload"] = dict(exit_evt["payload"], legal_proof_retained=False)
        bad_exit["event_id"] = "e-exit-bad"
        _, issues = validate_stream(base() + [bad_exit])
        self.assertTrue(any(i.code == "legal_proof_required" for i in issues))

    def test_marketing_withdrawal_does_not_block_reminders(self) -> None:
        withdrawal = event(
            "e-withdraw", "CONSENT_WITHDRAWN", "consent_grant", "g1", "2026-09-20T09:30:00+08:00", 2,
            {"grant_id": "g1", "scopes_withdrawn": ["marketing_use"], "withdrawn_at": "2026-09-20T09:30:00+08:00"},
        )
        grant = consent()
        grant["payload"]["scopes"].append("marketing_use")
        events = [grant, withdrawal, qualified(), plan_v1(),
                  reminder("ob1", at="2026-09-20T10:00:00+08:00")]
        _, issues = validate_stream(events)
        self.assertEqual([], [i for i in issues if i.code == "consent_required"])

    def test_revoked_qualification_blocks_signoff(self) -> None:
        revoke = event(
            "e-revoke", "QUALIFICATION_REVOKED", "pharmacist_profile", "P1",
            "2026-09-20T10:10:00+08:00", 2,
            {"pharmacist_id": "P1", "store_id": "A", "revoked_at": "2026-09-20T10:10:00+08:00"},
        )
        events = base() + [
            revoke,
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            followup("f1", at="2026-09-20T10:20:00+08:00"),
            confirm("ob1", "f1", at="2026-09-20T10:50:00+08:00"),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "qualification_required" for i in issues))

    def test_handoff_requires_cross_store_consent(self) -> None:
        events = base(scopes=["care_profile", "medication_reminder", "adherence_followup"]) + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            event("e-h-prop", "HANDOFF_PROPOSED", "continuity_handoff", "h1",
                  "2026-09-21T10:00:00+08:00", 1,
                  {"handoff_id": "h1", "subject_id": "S1", "obligation_ids": ["ob1"],
                   "from_store": "A", "to_store": "B", "proposed_at": "2026-09-21T10:00:00+08:00"}),
            event("e-h-done", "HANDOFF_COMPLETED", "continuity_handoff", "h1",
                  "2026-09-21T11:00:00+08:00", 2,
                  {"handoff_id": "h1", "subject_id": "S1", "obligation_ids": ["ob1"],
                   "from_store": "A", "to_store": "B", "completed_at": "2026-09-21T11:00:00+08:00"}),
        ]
        _, issues = validate_stream(events)
        self.assertTrue(any(i.code == "consent_required" for i in issues))

    def test_replay_resumes_from_saved_snapshot_mid_handoff(self) -> None:
        events = base() + [
            reminder("ob1", at="2026-09-20T10:00:00+08:00"),
            event("e-a-out", "STORE_WITHDRAWN", "store", "A", "2026-09-21T09:00:00+08:00", 1,
                  {"store_id": "A", "effective_at": "2026-09-21T09:00:00+08:00", "reason": "store_closed"}),
            event("e-h-prop", "HANDOFF_PROPOSED", "continuity_handoff", "h1", "2026-09-21T10:00:00+08:00", 1,
                  {"handoff_id": "h1", "subject_id": "S1", "obligation_ids": ["ob1"],
                   "from_store": "A", "to_store": "B", "proposed_at": "2026-09-21T10:00:00+08:00"}),
        ]
        rest = [
            event("e-h-done", "HANDOFF_COMPLETED", "continuity_handoff", "h1", "2026-09-21T11:00:00+08:00", 2,
                  {"handoff_id": "h1", "subject_id": "S1", "obligation_ids": ["ob1"],
                   "from_store": "A", "to_store": "B", "completed_at": "2026-09-21T11:00:00+08:00"}),
        ]
        state1, issues1 = fold_events(events)
        # 进程在交接中断：保存状态
        saved = json.loads(json.dumps(state1.snapshot(), ensure_ascii=False))
        restored = StreamState.restore(saved)
        state2, issues2 = fold_events(rest, restored)
        full_state, full_issues = validate_stream(events + rest)
        self.assertEqual([], issues1)
        self.assertEqual([], issues2)
        self.assertEqual([], full_issues)
        self.assertEqual("B", state2.obligations["ob1"].owner_store)
        self.assertEqual(full_state.obligations["ob1"].owner_store, state2.obligations["ob1"].owner_store)
        self.assertEqual(full_state.next_version, state2.next_version)


if __name__ == "__main__":
    unittest.main()
