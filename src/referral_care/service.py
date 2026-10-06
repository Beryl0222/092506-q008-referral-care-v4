"""转诊协同应用服务：状态流、幂等回执、超时升级、字段级可见性与审计链。"""
from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from .domain import (
    ADMIN_FIELDS,
    AUTO_RETURNED,
    OVERDUE_SIDE,
    ORIGIN_FIELDS,
    PENDING_ACCEPT,
    RECEIVER_FIELDS,
    RECEIVER_REVOKED_STATES,
    RECEIVER_SENSITIVE,
    TERMINAL_STATES,
    TRANSITIONS,
    DuplicateActive,
    Event,
    Forbidden,
    NotFound,
    Record,
    Referral,
    StateConflict,
    TokenInvalid,
    Validation,
    chain_hash,
    mask_sensitive,
)
from .store import Store

SYSTEM = "system"


class Service:
    def __init__(
        self,
        store: Store | None = None,
        *,
        clock=None,
        promise_seconds: int = 24 * 3600,
        escalation_seconds: int = 3600,
        supplement_seconds: int = 12 * 3600,
        max_reminders: int = 1,
    ) -> None:
        self.store = store or Store()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.promise_seconds = promise_seconds        # 接收方承诺回应时限
        self.escalation_seconds = escalation_seconds  # 提醒后多久升级为自动退回
        self.supplement_seconds = supplement_seconds  # 补充材料承诺时限
        self.max_reminders = max_reminders

    # ------------------------------------------------------------ 基础能力（兼容既有接口）

    def health(self) -> dict[str, str]:
        return {"service": "referral_care", "status": "ok"}

    def register(self, record_id: str, owner_id: str) -> dict[str, str]:
        record = self.store.save(Record(record_id, owner_id))
        return {"record_id": record.record_id, "owner_id": record.owner_id,
                "state": record.state, "created_at": record.created_at}

    def find(self, record_id: str) -> dict[str, str] | None:
        record = self.store.get(record_id)
        return record.__dict__.copy() if record else None

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------ 时间与内部工具

    def _now(self) -> datetime:
        moment = self.clock()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return self._now().isoformat()

    def _plus(self, seconds: int) -> str:
        return (self._now() + timedelta(seconds=seconds)).isoformat()

    def _must_get(self, case_id: str) -> Referral:
        referral = self.store.get_referral(case_id)
        if referral is None:
            raise NotFound(f"转诊单不存在: {case_id}")
        return referral

    def _append_event(self, case_id: str, event_type: str, actor_org: str,
                      actor_role: str, detail: dict, op_id: str = "") -> Event:
        prev = self.store.last_event_hash(case_id)
        created = self._now_iso()
        payload = {
            "case_id": case_id, "event_type": event_type, "actor_org": actor_org,
            "actor_role": actor_role, "detail": detail, "op_id": op_id, "created_at": created,
        }
        event = Event(
            case_id=case_id, event_type=event_type, actor_org=actor_org,
            actor_role=actor_role, detail=detail, op_id=op_id,
            prev_hash=prev, hash=chain_hash(prev, payload), created_at=created,
        )
        return self.store.append_event(event)

    def _enqueue(self, case_id: str, kind: str, target_org: str, payload: dict) -> None:
        self.store.enqueue(
            uuid.uuid4().hex, case_id, kind, target_org, payload, self._now_iso()
        )

    def _execute(self, action: str, op_id: str, case_id: str, perform):
        """幂等执行：同一 op_id 重复提交（网络重试/重复回执）返回首次结果并留痕。"""
        if not op_id:
            raise Validation("缺少幂等操作号 op_id")
        with self.store.transaction():
            prior = self.store.get_op(op_id)
            if prior is not None:
                self._append_event(
                    case_id, "duplicate_ignored", SYSTEM, SYSTEM,
                    {"action": action, "repeated_op": op_id},
                )
                return json.loads(prior["result"])
            result = perform()
            self.store.put_op(
                op_id, case_id, action,
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                self._now_iso(),
            )
            return result

    def _apply(self, referral: Referral, action: str, actor_org: str, op_id: str,
               event_type: str, detail: dict, *, updates: dict | None = None,
               notifications: tuple = ()) -> Referral:
        allowed, target, side = TRANSITIONS[action]
        expected = referral.origin_org if side == "origin" else referral.receiving_org
        if actor_org != expected:
            who = "转出方" if side == "origin" else "接收方"
            raise Forbidden(f"{action} 只能由{who}机构执行")
        if referral.state not in allowed:
            raise StateConflict(f"当前状态 {referral.state} 不允许执行 {action}")
        new_values = {
            "state": target,
            "updated_at": self._now_iso(),
            "version": referral.version + 1,
        }
        if target in TERMINAL_STATES:
            # 进入终态即吊销全部旧链接
            new_values["token_version"] = referral.token_version + 1
        if updates:
            new_values.update(updates)
        new_referral = replace(referral, **new_values)
        self.store.update_referral(new_referral, expected_version=referral.version)
        if target in TERMINAL_STATES:
            self.store.revoke_tokens(referral.case_id)
        self._append_event(referral.case_id, event_type, actor_org, side, detail, op_id=op_id)
        for kind, target_org, payload in notifications:
            self._enqueue(referral.case_id, kind, target_org, payload)
        return new_referral

    @staticmethod
    def _clear_deadline() -> dict:
        return {"respond_by": "", "escalate_by": "", "reminders_sent": 0}

    def _receiver_deadline(self) -> dict:
        return {"respond_by": self._plus(self.promise_seconds),
                "escalate_by": "", "reminders_sent": 0}

    def _origin_deadline(self) -> dict:
        return {"respond_by": self._plus(self.supplement_seconds),
                "escalate_by": "", "reminders_sent": 0}

    # ------------------------------------------------------------ 字段级可见性

    @staticmethod
    def _resolve_role(referral: Referral, viewer_org: str, viewer_role: str) -> str:
        if viewer_role == "admin":
            return "admin"
        if viewer_org == referral.origin_org:
            return "origin"
        if viewer_org == referral.receiving_org:
            return "receiver"
        return "outsider"

    @staticmethod
    def _view_for(referral: Referral, role: str) -> dict:
        if role == "origin":
            fields = ORIGIN_FIELDS
        elif role == "receiver":
            fields = RECEIVER_FIELDS
            if referral.state in RECEIVER_REVOKED_STATES:
                fields = tuple(f for f in fields if f not in RECEIVER_SENSITIVE)
        elif role == "admin":
            fields = ADMIN_FIELDS
        else:
            raise Forbidden("无权查看该转诊单")
        return {name: getattr(referral, name) for name in fields}

    def _log_access(self, case_id: str, viewer_org: str, viewer_role: str,
                    action: str, fields: tuple, via: str) -> None:
        self.store.add_access(case_id, viewer_org, viewer_role, action,
                              tuple(fields), via, self._now_iso())

    # ------------------------------------------------------------ 转诊生命周期

    def create_referral(self, *, op_id: str, case_id: str, patient_ref: str,
                        origin_org: str, receiving_org: str, purpose: str,
                        summary: str = "", contact_name: str = "", contact_phone: str = "",
                        internal_note: str = "", promise_seconds: int | None = None) -> dict:
        """转出：登记脱敏摘要，生成接收方链接，进入待接收状态。"""
        def perform() -> dict:
            missing = [name for name, value in (
                ("case_id", case_id), ("patient_ref", patient_ref),
                ("origin_org", origin_org), ("receiving_org", receiving_org),
                ("purpose", purpose),
            ) if not value]
            if missing:
                raise Validation(f"缺少必要字段: {', '.join(missing)}")
            if origin_org == receiving_org:
                raise Validation("转出方与接收方不能是同一机构")
            conflict = self.store.find_active_conflict(patient_ref, receiving_org, purpose)
            if conflict:
                raise DuplicateActive(
                    f"患者 {patient_ref} 在 {receiving_org} 已有进行中的「{purpose}」转诊: {conflict}"
                )
            now = self._now_iso()
            referral = Referral(
                case_id=case_id, patient_ref=patient_ref, origin_org=origin_org,
                receiving_org=receiving_org, purpose=purpose,
                state=PENDING_ACCEPT,
                summary=mask_sensitive(summary),
                internal_note=internal_note,
                contact_name=contact_name, contact_phone=contact_phone,
                respond_by=self._plus(promise_seconds or self.promise_seconds),
                created_at=now, updated_at=now,
            )
            self.store.insert_referral(referral)
            token = secrets.token_urlsafe(24)
            self.store.insert_token(token, case_id, "receiving", referral.token_version, now)
            self._append_event(case_id, "referral_created", origin_org, "origin",
                               {"purpose": purpose, "respond_by": referral.respond_by},
                               op_id=op_id)
            self._enqueue(case_id, "transfer_notice", receiving_org,
                          {"case_id": case_id, "purpose": purpose, "origin_org": origin_org})
            return {
                "case": self._view_for(referral, "origin"),
                "link_token": token,
                "link_path": f"/links/{token}",
            }

        return self._execute("create_referral", op_id, case_id, perform)

    def accept(self, op_id: str, case_id: str, actor_org: str) -> dict:
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "accept", actor_org, op_id, "accepted",
                {"responded_before": referral.respond_by},
                updates=self._clear_deadline(),
                notifications=(("receipt", referral.origin_org,
                                {"case_id": case_id, "state": "accepted"}),),
            )
            return {"case": self._view_for(new, "receiver")}
        return self._execute("accept", op_id, case_id, perform)

    def reject(self, op_id: str, case_id: str, actor_org: str, reason: str = "") -> dict:
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "reject", actor_org, op_id, "rejected",
                {"reason": mask_sensitive(reason)},
                updates={**self._clear_deadline(), "reject_reason": mask_sensitive(reason)},
                notifications=(("receipt", referral.origin_org,
                                {"case_id": case_id, "state": "rejected"}),),
            )
            return {"case": self._view_for(new, "receiver")}
        return self._execute("reject", op_id, case_id, perform)

    def request_supplement(self, op_id: str, case_id: str, actor_org: str, note: str = "") -> dict:
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "request_supplement", actor_org, op_id, "supplement_requested",
                {"note": mask_sensitive(note), "supplement_by": self._plus(self.supplement_seconds)},
                updates=self._origin_deadline(),
                notifications=(("supplement_request", referral.origin_org,
                                {"case_id": case_id, "note": mask_sensitive(note)}),),
            )
            return {"case": self._view_for(new, "receiver")}
        return self._execute("request_supplement", op_id, case_id, perform)

    def supplement(self, op_id: str, case_id: str, actor_org: str, extra_summary: str = "") -> dict:
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "supplement", actor_org, op_id, "supplemented",
                {"respond_by": self._plus(self.promise_seconds)},
                updates={**self._receiver_deadline(),
                         "extra_summary": mask_sensitive(extra_summary)},
                notifications=(("supplement_notice", referral.receiving_org,
                                {"case_id": case_id}),),
            )
            return {"case": self._view_for(new, "origin")}
        return self._execute("supplement", op_id, case_id, perform)

    def admit(self, op_id: str, case_id: str, actor_org: str) -> dict:
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "admit", actor_org, op_id, "admitted", {},
                updates=self._clear_deadline(),
                notifications=(("admit_notice", referral.origin_org,
                                {"case_id": case_id}),),
            )
            return {"case": self._view_for(new, "receiver")}
        return self._execute("admit", op_id, case_id, perform)

    def return_back(self, op_id: str, case_id: str, actor_org: str, return_summary: str = "") -> dict:
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "return_back", actor_org, op_id, "returned", {},
                updates={**self._clear_deadline(),
                         "return_summary": mask_sensitive(return_summary)},
                notifications=(("return_notice", referral.origin_org,
                                {"case_id": case_id}),),
            )
            return {"case": self._view_for(new, "receiver")}
        return self._execute("return_back", op_id, case_id, perform)

    def confirm_return(self, op_id: str, case_id: str, actor_org: str) -> dict:
        """原机构确认收到回转，转诊闭环。"""
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "confirm_return", actor_org, op_id, "return_confirmed", {},
                updates=self._clear_deadline(),
                notifications=(("loop_closed", referral.receiving_org,
                                {"case_id": case_id}),),
            )
            return {"case": self._view_for(new, "origin")}
        return self._execute("confirm_return", op_id, case_id, perform)

    def withdraw(self, op_id: str, case_id: str, actor_org: str, reason: str = "") -> dict:
        """撤回：立即吊销全部旧链接，接收方敏感授权同步收回。"""
        def perform() -> dict:
            referral = self._must_get(case_id)
            new = self._apply(
                referral, "withdraw", actor_org, op_id, "withdrawn",
                {"reason": mask_sensitive(reason)},
                updates=self._clear_deadline(),
                notifications=(("withdrawal_notice", referral.receiving_org,
                                {"case_id": case_id}),),
            )
            return {"case": self._view_for(new, "origin")}
        return self._execute("withdraw", op_id, case_id, perform)

    def change_contact(self, op_id: str, case_id: str, actor_org: str,
                       contact_name: str, contact_phone: str) -> dict:
        """联系人变更：不改变状态，但必须可审计。"""
        def perform() -> dict:
            referral = self._must_get(case_id)
            if actor_org != referral.origin_org:
                raise Forbidden("只有转出方能变更转诊联系人")
            if referral.state in TERMINAL_STATES:
                raise StateConflict(f"转诊单已终态（{referral.state}），不能变更联系人")
            new = replace(
                referral, contact_name=contact_name, contact_phone=contact_phone,
                updated_at=self._now_iso(), version=referral.version + 1,
            )
            self.store.update_referral(new, expected_version=referral.version)
            self._append_event(
                case_id, "contact_changed", actor_org, "origin",
                {"old_contact": mask_sensitive(f"{referral.contact_name} {referral.contact_phone}"),
                 "new_contact": mask_sensitive(f"{contact_name} {contact_phone}")},
                op_id=op_id,
            )
            self._enqueue(case_id, "contact_changed", referral.receiving_org,
                          {"case_id": case_id})
            return {"case": self._view_for(new, "origin")}
        return self._execute("change_contact", op_id, case_id, perform)

    # ------------------------------------------------------------ 超时提醒与升级

    def run_timeouts(self) -> list[dict]:
        """扫描承诺时限：先自动提醒，仍未回应则升级退回原机构。重启后可直接续跑。"""
        now_iso = self._now_iso()
        taken = []
        with self.store.transaction():
            for referral in self.store.list_deadline_cases():
                if referral.reminders_sent < self.max_reminders:
                    if referral.respond_by and referral.respond_by < now_iso:
                        side = OVERDUE_SIDE[referral.state]
                        target_org = (referral.receiving_org if side == "receiver"
                                      else referral.origin_org)
                        new = replace(
                            referral,
                            reminders_sent=referral.reminders_sent + 1,
                            escalate_by=self._plus(self.escalation_seconds),
                            updated_at=now_iso, version=referral.version + 1,
                        )
                        self.store.update_referral(new, expected_version=referral.version)
                        self._append_event(
                            referral.case_id, "reminder_sent", SYSTEM, SYSTEM,
                            {"reminder_no": new.reminders_sent,
                             "overdue_since": referral.respond_by, "notify": target_org},
                        )
                        self._enqueue(referral.case_id, "reminder", target_org,
                                      {"case_id": referral.case_id,
                                       "reminder_no": new.reminders_sent})
                        taken.append({"case_id": referral.case_id, "action": "reminder",
                                      "notify": target_org})
                elif referral.escalate_by and referral.escalate_by < now_iso:
                    new = replace(
                        referral, state=AUTO_RETURNED, respond_by="", escalate_by="",
                        token_version=referral.token_version + 1,
                        updated_at=now_iso, version=referral.version + 1,
                    )
                    self.store.update_referral(new, expected_version=referral.version)
                    self.store.revoke_tokens(referral.case_id)
                    self._append_event(referral.case_id, "timeout_escalated", SYSTEM, SYSTEM,
                                       {"reason": "承诺时限内未回应",
                                        "state_before": referral.state})
                    self._append_event(referral.case_id, "auto_returned", SYSTEM, SYSTEM,
                                       {"returned_to": referral.origin_org})
                    for org in (referral.origin_org, referral.receiving_org):
                        self._enqueue(referral.case_id, "escalation_notice", org,
                                      {"case_id": referral.case_id, "state": AUTO_RETURNED})
                    taken.append({"case_id": referral.case_id, "action": "auto_return",
                                  "notify": referral.origin_org})
        return taken

    # ------------------------------------------------------------ 查询与授权视图

    def view_case(self, case_id: str, viewer_org: str, viewer_role: str = "auto") -> dict:
        """按角色返回字段子集；每次查看（含拒绝）都写入访问记录。"""
        referral = self._must_get(case_id)
        role = self._resolve_role(referral, viewer_org, viewer_role)
        if role == "outsider":
            # 拒绝记录独立提交，不能随后续异常回滚
            with self.store.transaction():
                self._log_access(case_id, viewer_org, "outsider", "denied", (), "api")
            raise Forbidden("无权查看该转诊单")
        with self.store.transaction():
            view = self._view_for(referral, role)
            self._log_access(case_id, viewer_org, role, "view", tuple(view), "api")
            return view

    def issue_link(self, op_id: str, case_id: str, actor_org: str) -> dict:
        """为接收方补发链接；旧链接是否有效取决于 token_version。"""
        def perform() -> dict:
            referral = self._must_get(case_id)
            if actor_org not in (referral.origin_org, referral.receiving_org):
                raise Forbidden("只有转诊双方机构可以签发链接")
            if referral.state in TERMINAL_STATES:
                raise StateConflict("转诊单已终态，不能签发新链接")
            token = secrets.token_urlsafe(24)
            self.store.insert_token(token, case_id, "receiving",
                                    referral.token_version, self._now_iso())
            self._append_event(case_id, "link_issued", actor_org,
                               "origin" if actor_org == referral.origin_org else "receiver",
                               {"token_version": referral.token_version}, op_id=op_id)
            return {"link_token": token, "link_path": f"/links/{token}",
                    "token_version": referral.token_version}
        return self._execute("issue_link", op_id, case_id, perform)

    def open_link(self, token: str) -> dict:
        """通过链接查看：令牌被吊销或版本过期即失效。"""
        stored = self.store.get_token(token)
        if stored is None:
            raise TokenInvalid("链接不存在或已失效")
        referral = self._must_get(stored["case_id"])
        if stored["revoked"] or stored["token_version"] != referral.token_version:
            with self.store.transaction():
                self._log_access(referral.case_id, "unknown", "receiver", "denied", (), "link")
            raise TokenInvalid("链接已失效")
        with self.store.transaction():
            view = self._view_for(referral, "receiver")
            self._log_access(referral.case_id, referral.receiving_org, "receiver",
                             "view", tuple(view), "link")
            return view

    # ------------------------------------------------------------ 审计

    def list_events(self, case_id: str, viewer_org: str, viewer_role: str = "auto") -> list[dict]:
        referral = self._must_get(case_id)
        if self._resolve_role(referral, viewer_org, viewer_role) == "outsider":
            raise Forbidden("无权查看该转诊单的操作链")
        return [
            {"seq": e.seq, "event_type": e.event_type, "actor_org": e.actor_org,
             "actor_role": e.actor_role, "detail": e.detail, "op_id": e.op_id,
             "prev_hash": e.prev_hash, "hash": e.hash, "created_at": e.created_at}
            for e in self.store.list_events(case_id)
        ]

    def verify_chain(self, case_id: str) -> dict:
        """重放哈希链，核对操作链完整未被篡改。"""
        prev = ""
        events = self.store.list_events(case_id)
        valid = True
        for event in events:
            payload = {
                "case_id": event.case_id, "event_type": event.event_type,
                "actor_org": event.actor_org, "actor_role": event.actor_role,
                "detail": event.detail, "op_id": event.op_id, "created_at": event.created_at,
            }
            if event.prev_hash != prev or event.hash != chain_hash(prev, payload):
                valid = False
                break
            prev = event.hash
        return {"case_id": case_id, "valid": valid, "events": len(events)}

    def list_access(self, case_id: str, viewer_org: str, viewer_role: str = "auto") -> list[dict]:
        referral = self._must_get(case_id)
        role = self._resolve_role(referral, viewer_org, viewer_role)
        if role not in ("origin", "admin"):
            raise Forbidden("只有转出方或平台管理方可以查看访问记录")
        return self.store.list_access(case_id)

    # ------------------------------------------------------------ 通知投递（网络重试可审计）

    def flush_outbox(self, deliver=None) -> dict:
        """投递待发送通知；失败会记录重试事件，成功后记录已送达事件。"""
        deliver = deliver or (lambda notification: None)
        sent = failed = 0
        with self.store.transaction():
            for note in self.store.pending_notifications():
                attempts = note["attempts"] + 1
                payload = {**note, "payload": json.loads(note["payload"])}
                try:
                    deliver(payload)
                except Exception as exc:  # 网络故障等：留痕后等待下次重试
                    self.store.update_notification(
                        note["notification_id"], "pending", attempts,
                        str(exc), self._now_iso())
                    self._append_event(
                        note["case_id"], "notification_retry", SYSTEM, SYSTEM,
                        {"notification_id": note["notification_id"], "kind": note["kind"],
                         "target_org": note["target_org"], "attempt": attempts,
                         "error": str(exc)},
                    )
                    failed += 1
                else:
                    self.store.update_notification(
                        note["notification_id"], "sent", attempts, "", self._now_iso())
                    self._append_event(
                        note["case_id"], "notification_sent", SYSTEM, SYSTEM,
                        {"notification_id": note["notification_id"], "kind": note["kind"],
                         "target_org": note["target_org"], "attempt": attempts},
                    )
                    sent += 1
        return {"sent": sent, "failed": failed}
