"""转诊协同状态流与领域规则。

关键资料（病历摘要）在入库前完成脱敏，持久层只保存脱敏摘要与访问记录；
所有关键操作进入追加式哈希链事件日志，用于审计、超时升级与重启后核对。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone


# ---------------------------------------------------------------- 基础登记（兼容既有能力）

@dataclass(frozen=True)
class Record:
    record_id: str
    owner_id: str
    state: str = "draft"
    created_at: str = ""

    def with_timestamp(self) -> "Record":
        return Record(self.record_id, self.owner_id, self.state,
                      self.created_at or datetime.now(timezone.utc).isoformat())


# ---------------------------------------------------------------- 状态机

PENDING_ACCEPT = "pending_accept"            # 已转出，等待接收方回应
ACCEPTED = "accepted"                        # 接收方已接收
AWAITING_SUPPLEMENT = "awaiting_supplement"  # 接收方要求补充材料
SUPPLEMENTED = "supplemented"                # 转出方已补充材料，待接收方决定
ADMITTED = "admitted"                        # 接收方已接诊
RETURNED = "returned"                        # 接收方已回转，待原机构确认
CLOSED = "closed"                            # 闭环（终态）
REJECTED = "rejected"                        # 接收方拒绝（终态）
WITHDRAWN = "withdrawn"                      # 转出方撤回（终态）
AUTO_RETURNED = "auto_returned"              # 超时升级自动退回（终态）

TERMINAL_STATES = frozenset({CLOSED, REJECTED, WITHDRAWN, AUTO_RETURNED})
ACTIVE_STATES = frozenset(
    {PENDING_ACCEPT, ACCEPTED, AWAITING_SUPPLEMENT, SUPPLEMENTED, ADMITTED, RETURNED}
)

# 动作 -> (允许的源状态, 目标状态, 操作方 "origin"/"receiver")
TRANSITIONS = {
    "accept": (frozenset({PENDING_ACCEPT}), ACCEPTED, "receiver"),
    "reject": (frozenset({PENDING_ACCEPT}), REJECTED, "receiver"),
    "request_supplement": (frozenset({ACCEPTED, SUPPLEMENTED}), AWAITING_SUPPLEMENT, "receiver"),
    "supplement": (frozenset({AWAITING_SUPPLEMENT}), SUPPLEMENTED, "origin"),
    "admit": (frozenset({ACCEPTED, SUPPLEMENTED}), ADMITTED, "receiver"),
    "return_back": (frozenset({ADMITTED}), RETURNED, "receiver"),
    "confirm_return": (frozenset({RETURNED}), CLOSED, "origin"),
    "withdraw": (
        frozenset({PENDING_ACCEPT, ACCEPTED, AWAITING_SUPPLEMENT, SUPPLEMENTED}),
        WITHDRAWN,
        "origin",
    ),
}

# 这些状态下承诺时限到期时，哪一方欠一个回应
OVERDUE_SIDE = {
    PENDING_ACCEPT: "receiver",
    SUPPLEMENTED: "receiver",
    AWAITING_SUPPLEMENT: "origin",
}


# ---------------------------------------------------------------- 领域错误

class ReferralError(Exception):
    """所有领域错误的基类，code 供接口层映射。"""

    code = "referral_error"


class NotFound(ReferralError):
    code = "not_found"


class StateConflict(ReferralError):
    code = "state_conflict"


class Forbidden(ReferralError):
    code = "forbidden"


class DuplicateActive(ReferralError):
    code = "duplicate_active"


class TokenInvalid(ReferralError):
    code = "token_invalid"


class Validation(ReferralError):
    code = "validation"


# ---------------------------------------------------------------- 脱敏

_ID_RE = re.compile(r"(?<![0-9A-Za-z])(\d{6})\d{8}(\d{3}[0-9Xx])(?![0-9A-Za-z])")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_CARD_RE = re.compile(r"(?<!\d)(\d{4})\d{8,11}(\d{4})(?!\d)")
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@)")


def mask_sensitive(text: str | None) -> str:
    """入库前脱敏：身份证、手机号、长卡号、邮箱只保留首尾片段。"""
    if not text:
        return ""
    text = _ID_RE.sub(r"\1********\2", text)
    text = _PHONE_RE.sub(r"\1****\2", text)
    text = _CARD_RE.sub(r"\1********\2", text)
    text = _EMAIL_RE.sub(r"\1***\2", text)
    return text


# ---------------------------------------------------------------- 记录对象

@dataclass(frozen=True)
class Referral:
    case_id: str
    patient_ref: str          # 调用方传入的匿名化患者引用，不存原始证件号
    origin_org: str
    receiving_org: str
    purpose: str
    state: str
    summary: str = ""         # 脱敏后的病历摘要
    extra_summary: str = ""   # 补充材料（脱敏后）
    internal_note: str = ""   # 转出方内部备注，接收方不可见
    return_summary: str = ""  # 回转小结（脱敏后）
    reject_reason: str = ""
    contact_name: str = ""
    contact_phone: str = ""
    respond_by: str = ""      # 当前承诺回应时限（ISO 时间）
    escalate_by: str = ""     # 提醒后升级时限
    reminders_sent: int = 0
    token_version: int = 1    # 撤回/终态时递增，旧链接立即失效
    version: int = 0          # 乐观锁
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Event:
    case_id: str
    event_type: str
    actor_org: str
    actor_role: str
    detail: dict
    op_id: str
    prev_hash: str
    hash: str
    created_at: str
    seq: int = 0


def chain_hash(prev_hash: str, payload: dict) -> str:
    """事件哈希链：hash = sha256(prev_hash + 规范化JSON)。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 字段可见性

BASE_FIELDS = (
    "case_id", "state", "purpose", "origin_org", "receiving_org",
    "respond_by", "reminders_sent", "created_at", "updated_at",
)
# 平台管理方只看流转元数据，看不到任何病历摘要与联系方式
ADMIN_FIELDS = BASE_FIELDS + ("patient_ref", "token_version")
ORIGIN_FIELDS = BASE_FIELDS + (
    "patient_ref", "summary", "extra_summary", "internal_note",
    "return_summary", "reject_reason", "contact_name", "contact_phone", "token_version",
)
RECEIVER_FIELDS = BASE_FIELDS + (
    "patient_ref", "summary", "extra_summary",
    "return_summary", "reject_reason", "contact_name", "contact_phone",
)
# 撤回或超时退回后，接收方授权即被收回的敏感字段
RECEIVER_SENSITIVE = ("patient_ref", "summary", "extra_summary", "contact_name", "contact_phone")
RECEIVER_REVOKED_STATES = frozenset({WITHDRAWN, AUTO_RETURNED})
