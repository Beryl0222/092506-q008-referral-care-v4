"""状态流闭环、超时升级、服务重启与通知重试的验收测试。"""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from referral_care.domain import (
    ACCEPTED,
    AUTO_RETURNED,
    CLOSED,
    PENDING_ACCEPT,
    REJECTED,
    WITHDRAWN,
    Forbidden,
    StateConflict,
    TokenInvalid,
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


def make_service(clock, **kwargs):
    options = {"promise_seconds": 3600, "escalation_seconds": 1800,
               "supplement_seconds": 900}
    options.update(kwargs)
    return Service(Store(), clock=clock, **options)


def create_case(service, case_id="case-1", **overrides):
    params = dict(
        op_id=f"op-create-{case_id}", case_id=case_id, patient_ref="pat-001",
        origin_org="clinic-a", receiving_org="hospital-b", purpose="心内科会诊",
        summary="患者电话13812345678，初步诊断高血压",
        contact_name="王医生", contact_phone="13900001111",
        internal_note="内部备注：优先安排",
    )
    params.update(overrides)
    return service.create_referral(**params)


def event_types(service, case_id, viewer="clinic-a"):
    return [e["event_type"] for e in service.list_events(case_id, viewer)]


class 完整闭环测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def test_转出到回转的完整闭环(self):
        created = create_case(self.service)
        self.assertEqual(created["case"]["state"], PENDING_ACCEPT)
        self.assertIn("link_token", created)

        self.service.accept("op-accept-1", "case-1", "hospital-b")
        self.service.request_supplement("op-req-1", "case-1", "hospital-b", "请补充近期心电图")
        self.service.supplement("op-sup-1", "case-1", "clinic-a", "心电图见附图，电话13911112222")
        self.service.admit("op-admit-1", "case-1", "hospital-b")
        self.service.return_back("op-ret-1", "case-1", "hospital-b", "治疗完成，建议基层随访")
        final = self.service.confirm_return("op-confirm-1", "case-1", "clinic-a")

        self.assertEqual(final["case"]["state"], CLOSED)
        self.assertEqual(
            event_types(self.service, "case-1"),
            ["referral_created", "accepted", "supplement_requested", "supplemented",
             "admitted", "returned", "return_confirmed"],
        )
        chain = self.service.verify_chain("case-1")
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["events"], 7)

    def test_操作链哈希可检出篡改(self):
        create_case(self.service)
        self.service.accept("op-accept-1", "case-1", "hospital-b")
        self.assertTrue(self.service.verify_chain("case-1")["valid"])
        # 直接改库模拟篡改
        self.service.store.connection.execute(
            "UPDATE case_event SET actor_org='evil' WHERE case_id='case-1' AND event_type='accepted'"
        )
        self.assertFalse(self.service.verify_chain("case-1")["valid"])

    def test_接收方拒绝进入终态并通知转出方(self):
        create_case(self.service)
        result = self.service.reject("op-rej-1", "case-1", "hospital-b", "床位不足")
        self.assertEqual(result["case"]["state"], REJECTED)
        origin_view = self.service.view_case("case-1", "clinic-a")
        self.assertEqual(origin_view["reject_reason"], "床位不足")
        self.assertIn("rejected", event_types(self.service, "case-1"))
        notes = self.service.store.list_notifications("case-1")
        self.assertIn(("receipt", "clinic-a"), {(n["kind"], n["target_org"]) for n in notes})

    def test_撤回后旧链接立即失效(self):
        created = create_case(self.service)
        token = created["link_token"]
        self.assertEqual(self.service.open_link(token)["case_id"], "case-1")

        self.service.withdraw("op-wd-1", "case-1", "clinic-a", "患者自行前往上级医院")
        with self.assertRaises(TokenInvalid):
            self.service.open_link(token)
        # 补发链接也被禁止（已终态）
        with self.assertRaises(StateConflict):
            self.service.issue_link("op-link-2", "case-1", "clinic-a")
        # 接收方不再能看到敏感字段，转出方仍可看
        receiver_view = self.service.view_case("case-1", "hospital-b")
        self.assertEqual(receiver_view["state"], WITHDRAWN)
        self.assertNotIn("summary", receiver_view)
        self.assertNotIn("contact_phone", receiver_view)
        self.assertIn("summary", self.service.view_case("case-1", "clinic-a"))

    def test_联系人变更可审计(self):
        create_case(self.service)
        self.service.change_contact("op-cc-1", "case-1", "clinic-a", "李医生", "13700003333")
        events = self.service.list_events("case-1", "clinic-a")
        change = [e for e in events if e["event_type"] == "contact_changed"]
        self.assertEqual(len(change), 1)
        self.assertIn("137****3333", change[0]["detail"]["new_contact"])
        self.assertNotIn("13700003333", change[0]["detail"]["new_contact"])
        view = self.service.view_case("case-1", "hospital-b")
        self.assertEqual(view["contact_name"], "李医生")
        # 接收方无权变更
        with self.assertRaises(Forbidden):
            self.service.change_contact("op-cc-2", "case-1", "hospital-b", "赵医生", "13600004444")


class 超时升级测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def test_接收方超时先提醒再升级退回(self):
        created = create_case(self.service)
        token = created["link_token"]

        self.clock.advance(seconds=3601)  # 超过承诺回应时限
        actions = self.service.run_timeouts()
        self.assertEqual(actions, [{"case_id": "case-1", "action": "reminder",
                                    "notify": "hospital-b"}])
        view = self.service.view_case("case-1", "clinic-a")
        self.assertEqual(view["state"], PENDING_ACCEPT)
        self.assertEqual(view["reminders_sent"], 1)
        self.assertIn("reminder_sent", event_types(self.service, "case-1"))
        reminders = [n for n in self.service.store.list_notifications("case-1")
                     if n["kind"] == "reminder"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["target_org"], "hospital-b")

        self.clock.advance(seconds=1801)  # 提醒后仍无回应，升级
        actions = self.service.run_timeouts()
        self.assertEqual(actions, [{"case_id": "case-1", "action": "auto_return",
                                    "notify": "clinic-a"}])
        view = self.service.view_case("case-1", "clinic-a")
        self.assertEqual(view["state"], AUTO_RETURNED)
        types = event_types(self.service, "case-1")
        self.assertIn("timeout_escalated", types)
        self.assertIn("auto_returned", types)
        # 升级退回后旧链接立即失效
        with self.assertRaises(TokenInvalid):
            self.service.open_link(token)
        # 再次扫描不会重复处理
        self.assertEqual(self.service.run_timeouts(), [])

    def test_补充材料超时提醒转出方(self):
        create_case(self.service)
        self.service.accept("op-accept-1", "case-1", "hospital-b")
        self.service.request_supplement("op-req-1", "case-1", "hospital-b", "请补充化验单")

        self.clock.advance(seconds=901)  # 超过补充材料时限
        actions = self.service.run_timeouts()
        self.assertEqual(actions, [{"case_id": "case-1", "action": "reminder",
                                    "notify": "clinic-a"}])

    def test_时限内回应不触发提醒(self):
        create_case(self.service)
        self.clock.advance(seconds=1800)
        self.service.accept("op-accept-1", "case-1", "hospital-b")
        self.clock.advance(seconds=7200)
        self.assertEqual(self.service.run_timeouts(), [])


class 服务重启恢复测试(unittest.TestCase):
    def test_重启后状态事件链与超时扫描全部恢复(self):
        clock = FakeClock()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "referral.db"
            first = Service(Store(db), clock=clock,
                            promise_seconds=3600, escalation_seconds=1800)
            create_case(first, "case-1")
            first.accept("op-accept-1", "case-1", "hospital-b")
            first.request_supplement("op-req-1", "case-1", "hospital-b", "请补充心电图")
            create_case(first, "case-2", patient_ref="pat-002", purpose="骨科转诊")
            first.close()

            # 模拟服务重启：同一数据库文件上的全新实例
            second = Service(Store(db), clock=clock,
                             promise_seconds=3600, escalation_seconds=1800)
            view = second.view_case("case-1", "clinic-a")
            self.assertEqual(view["state"], "awaiting_supplement")
            self.assertEqual(
                event_types(second, "case-1"),
                ["referral_created", "accepted", "supplement_requested"],
            )
            # 重启后继续走完流程
            second.supplement("op-sup-1", "case-1", "clinic-a", "心电图已附")
            second.admit("op-admit-1", "case-1", "hospital-b")
            second.return_back("op-ret-1", "case-1", "hospital-b", "已治疗")
            second.confirm_return("op-confirm-1", "case-1", "clinic-a")
            self.assertEqual(second.view_case("case-1", "clinic-a")["state"], CLOSED)
            self.assertTrue(second.verify_chain("case-1")["valid"])

            # 重启后超时扫描基于持久化的承诺时限继续工作
            second.close()
            clock.advance(seconds=3601)
            third = Service(Store(db), clock=clock,
                            promise_seconds=3600, escalation_seconds=1800)
            actions = third.run_timeouts()
            self.assertEqual(actions, [{"case_id": "case-2", "action": "reminder",
                                        "notify": "hospital-b"}])
            third.close()


class 通知重试审计测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def test_网络故障重试全程留痕(self):
        create_case(self.service)
        attempts = {"n": 0}

        def flaky(notification):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("网络抖动")

        self.assertEqual(self.service.flush_outbox(flaky), {"sent": 0, "failed": 1})
        self.assertEqual(self.service.flush_outbox(flaky), {"sent": 0, "failed": 1})
        self.assertEqual(self.service.flush_outbox(flaky), {"sent": 1, "failed": 0})

        types = event_types(self.service, "case-1")
        self.assertEqual(types.count("notification_retry"), 2)
        self.assertEqual(types.count("notification_sent"), 1)
        retries = [e for e in self.service.list_events("case-1", "clinic-a")
                   if e["event_type"] == "notification_retry"]
        self.assertEqual([e["detail"]["attempt"] for e in retries], [1, 2])
        self.assertIn("网络抖动", retries[0]["detail"]["error"])
        note = self.service.store.list_notifications("case-1")[0]
        self.assertEqual(note["status"], "sent")
        self.assertEqual(note["attempts"], 3)
        self.assertTrue(self.service.verify_chain("case-1")["valid"])

    def test_重复回执幂等且留痕(self):
        create_case(self.service)
        first = self.service.accept("op-accept-1", "case-1", "hospital-b")
        second = self.service.accept("op-accept-1", "case-1", "hospital-b")
        self.assertEqual(first, second)
        types = event_types(self.service, "case-1")
        self.assertEqual(types.count("accepted"), 1)
        self.assertEqual(types.count("duplicate_ignored"), 1)
        self.assertEqual(self.service.view_case("case-1", "clinic-a")["state"], ACCEPTED)


if __name__ == "__main__":
    unittest.main()
