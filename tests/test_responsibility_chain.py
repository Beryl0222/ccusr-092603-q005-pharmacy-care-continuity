from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pharmacy_care_continuity.chain import (
    ChainError,
    ResponsibilityChain,
    rebuild,
    SCOPE_REMINDER,
    SCOPE_FOLLOWUP,
    SCOPE_HANDOFF,
    SCOPE_MARKETING,
)
from pharmacy_care_continuity.contracts import validate_event

TZ = timezone(timedelta(hours=8))


def t(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


class ResponsibilityChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.schema = json.loads(
            (ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8")
        )
        self.saved: list[dict] = []
        self.chain = ResponsibilityChain(self.schema, sink=self.saved.append)
        self._bootstrap()

    def _bootstrap(self) -> None:
        c = self.chain
        c.record_consent(
            "cust",
            [SCOPE_REMINDER, SCOPE_FOLLOWUP, SCOPE_HANDOFF, SCOPE_MARKETING],
            True,
            "store-a",
            t(9),
        )
        for pid, store in (("p-a", "store-a"), ("p-b", "store-b"), ("p-v", "store-c")):
            c.register_qualification(pid, f"LIC-{pid}", t(8), t(20), "admin", t(8, 5))
            c.open_shift(pid, store, t(8), t(18))
        c.declare_source("cust", "med-x", "prescription", "p-a", t(9, 1))
        c.activate_plan(
            "cust",
            "plan-1",
            "p-a",
            "store-a",
            [
                {"key": "ob-1", "fact_kind": "medication_reminder",
                 "window_start": t(10), "window_end": t(11)},
                {"key": "ob-2", "fact_kind": "adherence_followup",
                 "window_start": t(15), "window_end": t(16)},
            ],
            basis_event_ids=[],
            at=t(9, 2),
        )

    # 授权 ------------------------------------------------------------------

    def test_unknown_scope_rejected(self) -> None:
        with self.assertRaises(ChainError):
            self.chain.record_consent("c2", ["not_a_scope"], True, "store-a", t(9))

    def test_withdrawn_scope_blocks_service(self) -> None:
        self.chain.withdraw_scope("cust", SCOPE_REMINDER, "cust", t(9, 30))
        with self.assertRaises(ChainError):
            self.chain.call_reminder("cust", "ob-1", t(10, 20))

    def test_withdrawing_extra_scope_cancels_only_matching_open_obligations(self) -> None:
        self.chain.withdraw_scope("cust", SCOPE_REMINDER, "cust", t(9, 30))
        self.assertEqual(self.chain.state.obligations["ob-1"].status, "cancelled")
        # 依从回访义务不属于提醒分项，仍保留。
        self.assertEqual(self.chain.state.obligations["ob-2"].status, "open")

    def test_legal_proof_still_issued_after_withdrawal_and_exit(self) -> None:
        self.chain.withdraw_scope("cust", SCOPE_MARKETING, "cust", t(12))
        self.chain.decide_exit("cust", "full_service", "cust", t(13), "done")
        self.chain.issue_legal_proof(
            "proof-1", "cust", "store-a", "purchase_record", t(9), t(12), "clerk", t(13, 5)
        )
        self.assertEqual(1, len(self.chain.state.proofs))

    # 药师资质与班次 --------------------------------------------------------

    def test_unqualified_pharmacist_cannot_sign(self) -> None:
        self.chain.register_qualification("p-x", "LIC-X", t(8), t(9, 30), "admin", t(8))
        self.chain.open_shift("p-x", "store-a", t(8), t(18))
        with self.assertRaises(ChainError):
            self.chain.confirm_service(
                "fact-ob-1", "cust", "ob-1", "p-x", "store-a", "taken", "x", t(10)
            )

    def test_off_duty_pharmacist_cannot_sign(self) -> None:
        with self.assertRaises(ChainError):
            self.chain.confirm_service(
                "fact-ob-1", "cust", "ob-1", "p-b", "store-a", "taken", "x", t(10)
            )

    def test_pharmacist_signs_only_personally_performed_once(self) -> None:
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        self.chain.confirm_service(fk, "cust", "ob-1", "p-a", "store-a", "taken", "ok", t(10, 30))
        with self.assertRaises(ChainError):
            self.chain.confirm_service(fk, "cust", "ob-1", "p-a", "store-a", "missed", "x", t(10, 40))

    # 计划与销售隔离 --------------------------------------------------------

    def test_plan_versions_must_be_sequential(self) -> None:
        with self.assertRaises(ChainError):
            self.chain.activate_plan(
                "cust", "plan-1", "p-a", "store-a", [], basis_event_ids=[],
                at=t(9, 5), supersedes=9,
            )

    def test_sales_offer_does_not_change_plan_and_never_diagnoses(self) -> None:
        # 即便携带优惠，计划义务与窗口保持不变，且声明不自动诊断。
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        self.chain.confirm_service(
            fk, "cust", "ob-1", "p-a", "store-a", "taken", "ok", t(10, 30),
            sales_offer_id="offer-9",
        )
        plan = next(
            e["payload"] for e in self.saved
            if e["event_type"] == "PLAN_ACTIVATED" and e["payload"]["plan_id"] == "plan-1"
        )
        self.assertIsNone(plan["sales_offer_id"])
        self.assertTrue(plan["diagnosis_declined"])
        self.assertEqual(2, len(plan["obligations"]))

    def test_reminder_only_inside_saved_window(self) -> None:
        with self.assertRaises(ChainError):
            self.chain.call_reminder("cust", "ob-1", t(12))

    # 跨店去重与矛盾核验 ----------------------------------------------------

    def _contradicted_fact(self) -> str:
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        self.chain.confirm_service(
            fk, "cust", "ob-1", "p-a", "store-a", "taken", "已服", t(10, 30),
            sales_offer_id="offer-9",
        )
        self.chain.confirm_service(
            fk, "cust", "ob-1", "p-b", "store-b", "missed", "未取", t(10, 45),
            sales_offer_id="offer-3",
        )
        return fk

    def test_same_fact_across_stores_is_one_service_fact(self) -> None:
        fk = self._contradicted_fact()
        self.assertEqual(2, len(self.chain.state.facts[fk].confirmations))
        self.assertEqual(1, len(self.chain.state.facts))

    def test_sales_involved_pharmacist_cannot_verify(self) -> None:
        fk = self._contradicted_fact()
        taken_id = next(
            c.event_id for c in self.chain.state.facts[fk].confirmations if c.outcome == "taken"
        )
        with self.assertRaises(ChainError):
            self.chain.resolve_discrepancy(fk, "p-a", taken_id, t(11))

    def test_independent_pharmacist_resolves_contradiction(self) -> None:
        fk = self._contradicted_fact()
        taken_id = next(
            c.event_id for c in self.chain.state.facts[fk].confirmations if c.outcome == "taken"
        )
        self.chain.resolve_discrepancy(fk, "p-v", taken_id, t(11))
        fact = self.chain.state.facts[fk]
        self.assertEqual("taken", fact.resolved_outcome)
        self.assertEqual("p-v", fact.resolver_pharmacist_id)

    def test_identical_records_merge_without_resolution(self) -> None:
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        self.chain.confirm_service(fk, "cust", "ob-1", "p-a", "store-a", "taken", "a", t(10, 30))
        # 第二家结论一致：归入同一服务事实，但不构成矛盾、无需核验。
        self.chain.confirm_service(fk, "cust", "ob-1", "p-b", "store-b", "taken", "b", t(10, 40))
        fact = self.chain.state.facts[fk]
        self.assertEqual(2, len(fact.confirmations))
        self.assertIsNone(fact.resolved_outcome)
        with self.assertRaises(ChainError):
            self.chain.resolve_discrepancy(fk, "p-v", fact.confirmations[0].event_id, t(11))

    # 异常升级 --------------------------------------------------------------

    def test_shortage_escalation_marks_obligation(self) -> None:
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        self.chain.escalate(
            fk, "cust", "ob-1", "p-a", "store-a", "drug_shortage", t(10, 40),
            evidence_event_ids=[],
        )
        self.assertEqual(self.chain.state.obligations["ob-1"].status, "escalated")
        self.assertEqual("open", self.chain.state.escalations[fk].status)

    # 门店承接：唯一成功、只迁移未履行义务 ----------------------------------

    def _withdraw_store_a(self) -> None:
        self.chain.withdraw_store("store-a", "store_closure", t(12))

    def test_only_open_obligations_are_transferable(self) -> None:
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        self.chain.confirm_service(fk, "cust", "ob-1", "p-a", "store-a", "taken", "ok", t(10, 30))
        self._withdraw_store_a()
        keys = [o.key for o in self.chain.transferable_obligations("cust", "store-a")]
        self.assertEqual(["ob-2"], keys)  # ob-1 已履行，不迁移

    def test_two_stores_cannot_both_accept_same_obligation(self) -> None:
        self._withdraw_store_a()
        self.chain.accept_handoff("ho-1", "cust", "store-a", "store-c", ["ob-2"], "p-v", t(12, 10))
        # 第二家后到，责任已不在 store-a，必然失败。
        with self.assertRaises(ChainError):
            self.chain.accept_handoff("ho-2", "cust", "store-a", "store-b", ["ob-2"], "p-b", t(12, 11))

    def test_cannot_hand_off_without_store_withdrawal(self) -> None:
        with self.assertRaises(ChainError):
            self.chain.accept_handoff("ho-1", "cust", "store-a", "store-c", ["ob-2"], "p-v", t(12))

    def test_handoff_requires_scope(self) -> None:
        self.chain.withdraw_scope("cust", SCOPE_HANDOFF, "cust", t(11))
        self._withdraw_store_a()
        with self.assertRaises(ChainError):
            self.chain.accept_handoff("ho-1", "cust", "store-a", "store-c", ["ob-2"], "p-v", t(12, 10))

    def test_obligation_continues_at_accepting_store(self) -> None:
        self._withdraw_store_a()
        self.chain.accept_handoff("ho-1", "cust", "store-a", "store-c", ["ob-2"], "p-v", t(12, 10))
        self.chain.complete_handoff("ho-1", t(12, 20))
        ob = self.chain.state.obligations["ob-2"]
        self.assertEqual("store-c", ob.store_id)
        self.assertEqual("p-v", ob.pharmacist_id)
        self.assertEqual("open", ob.status)  # 仍未履行，新店继续
        fk = self.chain.call_reminder("cust", "ob-2", t(15, 10))
        self.chain.confirm_service(fk, "cust", "ob-2", "p-v", "store-c", "taken", "ok", t(15, 20))

    # 应急库存与退出 --------------------------------------------------------

    def test_emergency_stock_commitment_validation(self) -> None:
        with self.assertRaises(ChainError):
            self.chain.commit_emergency_stock(
                "s-1", "cust", "store-c", "med-x", 0, "p-v", t(12, 30), t(18)
            )
        with self.assertRaises(ChainError):
            self.chain.commit_emergency_stock(
                "s-1", "cust", "store-c", "med-x", 2, "p-v", t(12, 30), t(11)
            )

    def test_exit_cancels_open_obligations(self) -> None:
        self.chain.decide_exit("cust", "full_service", "cust", t(13), "tired")
        self.assertEqual("cancelled", self.chain.state.obligations["ob-1"].status)
        self.assertEqual("cancelled", self.chain.state.obligations["ob-2"].status)
        with self.assertRaises(ChainError):
            self.chain.decide_exit("cust", "full_service", "cust", t(14), "again")

    # 受限接口 --------------------------------------------------------------

    def test_responsibility_view_fields_and_notice(self) -> None:
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        view = self.chain.responsibility_view("cust", "pharmacist")
        self.assertEqual("store-a", view.responsible_store)
        self.assertEqual("p-a", view.responsible_pharmacist)
        self.assertEqual(1, view.plan.plan_version)
        self.assertEqual(fk, view.reminders[0]["fact_key"])
        self.assertTrue(view.reminders[0]["evidence_event_ids"])  # 每次提醒有事实依据
        self.assertIn("不自动", view.diagnosis_notice)

    def test_staff_sees_minimal_information(self) -> None:
        self.chain.call_reminder("cust", "ob-1", t(10, 20))
        staff = self.chain.responsibility_view("cust", "staff")
        self.assertIsNone(staff.plan)
        self.assertEqual((), staff.reminders)
        self.assertEqual("cust", staff.customer_id)

    # 崩溃恢复与契约 --------------------------------------------------------

    def test_rebuild_resumes_from_saved_state_after_interruption(self) -> None:
        fk = self.chain.call_reminder("cust", "ob-1", t(10, 20))
        self.chain.confirm_service(fk, "cust", "ob-1", "p-a", "store-a", "taken", "ok", t(10, 30))
        self._withdraw_store_a()
        self.chain.accept_handoff("ho-1", "cust", "store-a", "store-c", ["ob-2"], "p-v", t(12, 10))

        rebuilt = rebuild(self.schema, self.saved)
        self.assertEqual(len(self.saved), len(rebuilt.state.log))
        self.assertEqual("store-c", rebuilt.state.obligations["ob-2"].store_id)
        self.assertEqual("confirmed", rebuilt.state.obligations["ob-1"].status)
        # 重建后继续：后到的第二家仍失败，窗口/升级按保存状态执行。
        with self.assertRaises(ChainError):
            rebuilt.accept_handoff("ho-2", "cust", "store-a", "store-b", ["ob-2"], "p-b", t(12, 11))
        fk2 = rebuilt.call_reminder("cust", "ob-2", t(15, 10))
        rebuilt.confirm_service(fk2, "cust", "ob-2", "p-v", "store-c", "taken", "ok", t(15, 20))

    def test_failed_sink_does_not_mutate_state(self) -> None:
        def boom(_event: dict) -> None:
            raise RuntimeError("存储中断")

        chain = ResponsibilityChain(self.schema, history=self.saved, sink=boom)
        before = len(chain.state.log)
        with self.assertRaises(RuntimeError):
            chain.withdraw_store("store-a", "store_closure", t(12))
        self.assertEqual(before, len(chain.state.log))

    def test_all_emitted_events_pass_exchange_contract(self) -> None:
        fk = self._contradicted_fact()
        taken_id = next(
            c.event_id for c in self.chain.state.facts[fk].confirmations if c.outcome == "taken"
        )
        self.chain.resolve_discrepancy(fk, "p-v", taken_id, t(11))
        self._withdraw_store_a()
        self.chain.accept_handoff("ho-1", "cust", "store-a", "store-c", ["ob-2"], "p-v", t(12, 10))
        self.chain.complete_handoff("ho-1", t(12, 20))
        self.chain.commit_emergency_stock("s-1", "cust", "store-c", "med-x", 2, "p-v", t(12, 30), t(18))
        self.chain.decide_exit("cust", "full_service", "cust", t(17), "bye")
        for event in self.saved:
            self.assertEqual([], validate_event(event, self.schema), event["event_type"])

    def test_duplicate_saved_event_is_rejected(self) -> None:
        with self.assertRaises(ChainError):
            rebuild(self.schema, [self.saved[0], self.saved[0]])


if __name__ == "__main__":
    unittest.main()
