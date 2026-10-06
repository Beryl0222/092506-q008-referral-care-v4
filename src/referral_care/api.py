"""供进程内调用的轻量请求适配层。

既有动作（health/register）保持原始返回形状；
转诊协同动作统一返回 {"ok": bool, "result"|"error": ...} 信封。
"""
import json

from .domain import ReferralError
from .service import Service


def _flush_outbox(service: Service, body: dict) -> dict:
    fail_kinds = set(body.get("fail_kinds", []))

    def deliver(notification):
        if notification["kind"] in fail_kinds:
            raise RuntimeError("模拟网络故障")

    return service.flush_outbox(deliver)


_ACTIONS = {
    "create_referral": lambda s, b: s.create_referral(
        op_id=str(b["op_id"]), case_id=str(b["case_id"]),
        patient_ref=str(b["patient_ref"]), origin_org=str(b["origin_org"]),
        receiving_org=str(b["receiving_org"]), purpose=str(b["purpose"]),
        summary=str(b.get("summary", "")),
        contact_name=str(b.get("contact_name", "")),
        contact_phone=str(b.get("contact_phone", "")),
        internal_note=str(b.get("internal_note", "")),
        promise_seconds=b.get("promise_seconds"),
    ),
    "accept": lambda s, b: s.accept(str(b["op_id"]), str(b["case_id"]), str(b["actor_org"])),
    "reject": lambda s, b: s.reject(str(b["op_id"]), str(b["case_id"]),
                                    str(b["actor_org"]), str(b.get("reason", ""))),
    "request_supplement": lambda s, b: s.request_supplement(
        str(b["op_id"]), str(b["case_id"]), str(b["actor_org"]), str(b.get("note", ""))),
    "supplement": lambda s, b: s.supplement(str(b["op_id"]), str(b["case_id"]),
                                            str(b["actor_org"]), str(b.get("extra_summary", ""))),
    "admit": lambda s, b: s.admit(str(b["op_id"]), str(b["case_id"]), str(b["actor_org"])),
    "return_back": lambda s, b: s.return_back(str(b["op_id"]), str(b["case_id"]),
                                              str(b["actor_org"]), str(b.get("return_summary", ""))),
    "confirm_return": lambda s, b: s.confirm_return(str(b["op_id"]), str(b["case_id"]),
                                                    str(b["actor_org"])),
    "withdraw": lambda s, b: s.withdraw(str(b["op_id"]), str(b["case_id"]),
                                        str(b["actor_org"]), str(b.get("reason", ""))),
    "change_contact": lambda s, b: s.change_contact(
        str(b["op_id"]), str(b["case_id"]), str(b["actor_org"]),
        str(b.get("contact_name", "")), str(b.get("contact_phone", ""))),
    "tick": lambda s, b: {"actions": s.run_timeouts()},
    "view_case": lambda s, b: s.view_case(str(b["case_id"]), str(b.get("viewer_org", "")),
                                          str(b.get("viewer_role", "auto"))),
    "issue_link": lambda s, b: s.issue_link(str(b["op_id"]), str(b["case_id"]),
                                            str(b["actor_org"])),
    "open_link": lambda s, b: s.open_link(str(b["token"])),
    "list_events": lambda s, b: s.list_events(str(b["case_id"]), str(b.get("viewer_org", "")),
                                              str(b.get("viewer_role", "auto"))),
    "list_access": lambda s, b: s.list_access(str(b["case_id"]), str(b.get("viewer_org", "")),
                                              str(b.get("viewer_role", "auto"))),
    "verify_chain": lambda s, b: s.verify_chain(str(b["case_id"])),
    "flush_outbox": _flush_outbox,
}


def handle(payload: str, service: Service | None = None) -> str:
    service = service or Service()
    body = json.loads(payload)
    action = body.get("action")
    if action == "health":
        return json.dumps(service.health(), ensure_ascii=False)
    if action == "register":
        return json.dumps(
            service.register(str(body["record_id"]), str(body["owner_id"])),
            ensure_ascii=False,
        )
    handler = _ACTIONS.get(action)
    if handler is None:
        raise ValueError("不支持的请求动作")
    try:
        result = handler(service, body)
    except ReferralError as exc:
        return json.dumps(
            {"ok": False, "error": {"code": exc.code, "message": str(exc)}},
            ensure_ascii=False,
        )
    return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
