"""多机构并发、重复回执幂等与同患者多转诊的验收测试。"""
import threading
import unittest
from datetime import datetime, timedelta, timezone

from referral_care.domain import (
    ACCEPTED,
    DuplicateActive,
    StateConflict,
)
from referral_care.service import Service
from referral_care.store import Store


class FakeClock:
    def __init__(self, start=datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)):
        self.moment = start

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


def make_service(clock):
    return Service(Store(), clock=clock, promise_seconds=3600,
                   escalation_seconds=1800, supplement_seconds=900)


def create_case(service, case_id, *, patient_ref="pat-001", origin_org="clinic-a",
                receiving_org="hospital-b", purpose="心内科会诊"):
    return service.create_referral(
        op_id=f"op-create-{case_id}", case_id=case_id, patient_ref=patient_ref,
        origin_org=origin_org, receiving_org=receiving_org, purpose=purpose,
        summary="脱敏前摘要", contact_name="王医生", contact_phone="13900001111",
    )


class 幂等回执测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)
        create_case(self.service, "case-1")

    def test_相同操作号重复提交返回首次结果(self):
        first = self.service.accept("op-accept-1", "case-1", "hospital-b")
        replay = self.service.accept("op-accept-1", "case-1", "hospital-b")
        self.assertEqual(first, replay)
        types = [e["event_type"] for e in self.service.list_events("case-1", "clinic-a")]
        self.assertEqual(types.count("accepted"), 1)
        self.assertEqual(types.count("duplicate_ignored"), 1)

    def test_不同操作号重复动作报状态冲突(self):
        self.service.accept("op-accept-1", "case-1", "hospital-b")
        with self.assertRaises(StateConflict):
            self.service.accept("op-accept-2", "case-1", "hospital-b")

    def test_创建操作幂等重放(self):
        replay = self.service.create_referral(
            op_id="op-create-case-1", case_id="case-1", patient_ref="pat-001",
            origin_org="clinic-a", receiving_org="hospital-b", purpose="心内科会诊",
        )
        self.assertEqual(replay["case"]["case_id"], "case-1")
        types = [e["event_type"] for e in self.service.list_events("case-1", "clinic-a")]
        self.assertEqual(types.count("referral_created"), 1)
        self.assertEqual(types.count("duplicate_ignored"), 1)


class 同患者多转诊测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def test_互不冲突的转诊可以并存(self):
        create_case(self.service, "case-1", receiving_org="hospital-b", purpose="心内科会诊")
        create_case(self.service, "case-2", receiving_org="hospital-c", purpose="心内科会诊")
        create_case(self.service, "case-3", receiving_org="hospital-b", purpose="骨科转诊")
        states = [self.service.view_case(c, "clinic-a")["state"]
                  for c in ("case-1", "case-2", "case-3")]
        self.assertEqual(states, ["pending_accept"] * 3)

    def test_同机构同目的的进行中转诊被拒绝(self):
        create_case(self.service, "case-1")
        with self.assertRaises(DuplicateActive):
            create_case(self.service, "case-2")

    def test_原转诊终态后同目的转诊放行(self):
        create_case(self.service, "case-1")
        self.service.reject("op-rej-1", "case-1", "hospital-b", "床位不足")
        create_case(self.service, "case-2")
        self.assertEqual(self.service.view_case("case-2", "clinic-a")["state"],
                         "pending_accept")


class 多机构并发测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def _run_threads(self, target, count):
        threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def test_并发接收同一转诊只有一个成功(self):
        create_case(self.service, "case-1")
        outcomes = []

        def worker(i):
            try:
                self.service.accept(f"op-accept-{i}", "case-1", "hospital-b")
                outcomes.append("ok")
            except StateConflict:
                outcomes.append("conflict")

        self._run_threads(worker, 8)
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("conflict"), 7)
        self.assertEqual(self.service.view_case("case-1", "clinic-a")["state"], ACCEPTED)
        types = [e["event_type"] for e in self.service.list_events("case-1", "clinic-a")]
        self.assertEqual(types.count("accepted"), 1)

    def test_并发创建冲突转诊只有一个成功(self):
        outcomes = []

        def worker(i):
            try:
                self.service.create_referral(
                    op_id=f"op-create-{i}", case_id=f"case-{i}",
                    patient_ref="pat-001", origin_org="clinic-a",
                    receiving_org="hospital-b", purpose="心内科会诊",
                )
                outcomes.append("ok")
            except DuplicateActive:
                outcomes.append("duplicate")

        self._run_threads(worker, 6)
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("duplicate"), 5)

    def test_多机构并发互不冲突转诊全部成功(self):
        outcomes = []

        def worker(i):
            try:
                self.service.create_referral(
                    op_id=f"op-create-{i}", case_id=f"case-{i}",
                    patient_ref="pat-001", origin_org="clinic-a",
                    receiving_org=f"hospital-{i}", purpose="心内科会诊",
                )
                outcomes.append("ok")
            except Exception:
                outcomes.append("error")

        self._run_threads(worker, 6)
        self.assertEqual(outcomes.count("ok"), 6)

    def test_并发重复回执全部幂等(self):
        create_case(self.service, "case-1")
        results = []

        def worker(i):
            # 多个机构网关重试同一个回执（相同操作号）
            results.append(self.service.accept("op-accept-1", "case-1", "hospital-b"))

        self._run_threads(worker, 5)
        self.assertTrue(all(r == results[0] for r in results))
        types = [e["event_type"] for e in self.service.list_events("case-1", "clinic-a")]
        self.assertEqual(types.count("accepted"), 1)
        self.assertEqual(types.count("duplicate_ignored"), 4)


if __name__ == "__main__":
    unittest.main()
