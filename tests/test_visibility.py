"""角色字段可见性、脱敏存储与访问记录的验收测试。"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from referral_care.api import handle
from referral_care.domain import (
    ADMIN_FIELDS,
    ORIGIN_FIELDS,
    RECEIVER_FIELDS,
    Forbidden,
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


def make_service(clock):
    return Service(Store(), clock=clock, promise_seconds=3600,
                   escalation_seconds=1800, supplement_seconds=900)


def create_case(service, case_id="case-1", **overrides):
    params = dict(
        op_id=f"op-create-{case_id}", case_id=case_id, patient_ref="pat-001",
        origin_org="clinic-a", receiving_org="hospital-b", purpose="心内科会诊",
        summary="患者张三，电话13812345678，身份证110101199003077777，诊断高血压",
        contact_name="王医生", contact_phone="13900001111",
        internal_note="内部备注：优先安排",
    )
    params.update(overrides)
    return service.create_referral(**params)


class 角色字段矩阵测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)
        create_case(self.service)

    def test_转出方看到全部授权字段(self):
        view = self.service.view_case("case-1", "clinic-a")
        self.assertEqual(set(view), set(ORIGIN_FIELDS))
        self.assertIn("internal_note", view)
        self.assertIn("summary", view)

    def test_接收方看不到转出方内部备注(self):
        view = self.service.view_case("case-1", "hospital-b")
        self.assertEqual(set(view), set(RECEIVER_FIELDS))
        self.assertNotIn("internal_note", view)
        self.assertIn("summary", view)

    def test_平台管理方只看流转元数据(self):
        view = self.service.view_case("case-1", "platform", "admin")
        self.assertEqual(set(view), set(ADMIN_FIELDS))
        for sensitive in ("summary", "extra_summary", "contact_name", "contact_phone",
                          "internal_note", "return_summary"):
            self.assertNotIn(sensitive, view)

    def test_无关第三方被拒绝并留痕(self):
        with self.assertRaises(Forbidden):
            self.service.view_case("case-1", "hospital-c")
        denied = [a for a in self.service.store.list_access("case-1")
                  if a["action"] == "denied"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["viewer_org"], "hospital-c")

    def test_查看动作写入访问记录(self):
        self.service.view_case("case-1", "clinic-a")
        self.service.view_case("case-1", "hospital-b")
        self.service.view_case("case-1", "platform", "admin")
        logs = self.service.list_access("case-1", "clinic-a")
        self.assertEqual(
            [(a["viewer_role"], a["via"]) for a in logs],
            [("origin", "api"), ("receiver", "api"), ("admin", "api")],
        )
        self.assertEqual(set(logs[0]["fields"]), set(ORIGIN_FIELDS))
        # 接收方无权查看访问记录
        with self.assertRaises(Forbidden):
            self.service.list_access("case-1", "hospital-b")


class 脱敏存储测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def test_摘要入库前完成脱敏(self):
        create_case(self.service)
        view = self.service.view_case("case-1", "clinic-a")
        self.assertIn("138****5678", view["summary"])
        self.assertIn("110101********7777", view["summary"])
        self.assertNotIn("13812345678", view["summary"])
        self.assertNotIn("110101199003077777", view["summary"])
        # 数据库里同样只存脱敏结果
        row = self.service.store.connection.execute(
            "SELECT summary FROM referral WHERE case_id='case-1'"
        ).fetchone()
        self.assertNotIn("13812345678", row["summary"])

    def test_补充材料与回转小结同样脱敏(self):
        create_case(self.service)
        self.service.accept("op-accept-1", "case-1", "hospital-b")
        self.service.request_supplement("op-req-1", "case-1", "hospital-b", "补充化验")
        self.service.supplement("op-sup-1", "case-1", "clinic-a",
                                "化验单联系电话13611112222")
        self.service.admit("op-admit-1", "case-1", "hospital-b")
        self.service.return_back("op-ret-1", "case-1", "hospital-b",
                                 "随访电话13522223333")
        view = self.service.view_case("case-1", "clinic-a")
        self.assertIn("136****2222", view["extra_summary"])
        self.assertIn("135****3333", view["return_summary"])


class 链接访问测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def test_链接查看按接收方授权并留痕(self):
        created = create_case(self.service)
        view = self.service.open_link(created["link_token"])
        self.assertEqual(set(view), set(RECEIVER_FIELDS))
        logs = self.service.store.list_access("case-1")
        self.assertEqual(logs[-1]["via"], "link")
        self.assertEqual(logs[-1]["viewer_role"], "receiver")

    def test_无效链接被拒绝(self):
        create_case(self.service)
        with self.assertRaises(TokenInvalid):
            self.service.open_link("不存在的令牌")


class 接口信封测试(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.service = make_service(self.clock)

    def call(self, body):
        return json.loads(handle(json.dumps(body), self.service))

    def test_成功信封(self):
        result = self.call({
            "action": "create_referral", "op_id": "op-1", "case_id": "case-1",
            "patient_ref": "pat-001", "origin_org": "clinic-a",
            "receiving_org": "hospital-b", "purpose": "心内科会诊",
            "summary": "电话13812345678",
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["case"]["state"], "pending_accept")

    def test_越权查看返回结构化错误(self):
        create_case(self.service)
        result = self.call({"action": "view_case", "case_id": "case-1",
                            "viewer_org": "hospital-c"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")

    def test_接口层完成一次闭环(self):
        create_case(self.service)
        steps = [
            {"action": "accept", "op_id": "op-a", "case_id": "case-1",
             "actor_org": "hospital-b"},
            {"action": "admit", "op_id": "op-ad", "case_id": "case-1",
             "actor_org": "hospital-b"},
            {"action": "return_back", "op_id": "op-r", "case_id": "case-1",
             "actor_org": "hospital-b", "return_summary": "已好转"},
            {"action": "confirm_return", "op_id": "op-c", "case_id": "case-1",
             "actor_org": "clinic-a"},
        ]
        for step in steps:
            self.assertTrue(self.call(step)["ok"], step)
        view = self.call({"action": "view_case", "case_id": "case-1",
                          "viewer_org": "clinic-a"})
        self.assertEqual(view["result"]["state"], "closed")
        chain = self.call({"action": "verify_chain", "case_id": "case-1"})
        self.assertTrue(chain["result"]["valid"])


if __name__ == "__main__":
    unittest.main()
