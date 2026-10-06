"""转诊协同的本地持久化边界（SQLite）。

单连接 + 可重入锁保证线程安全；所有多步写入必须包在 transaction() 里，
服务重启后从同一数据库文件恢复全部状态、事件链与通知箱。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .domain import (
    ACTIVE_STATES,
    DuplicateActive,
    Event,
    Record,
    Referral,
    StateConflict,
)

_ACTIVE_LIST = ",".join(f"'{s}'" for s in sorted(ACTIVE_STATES))

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS referral_case (
    record_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referral (
    case_id TEXT PRIMARY KEY,
    patient_ref TEXT NOT NULL,
    origin_org TEXT NOT NULL,
    receiving_org TEXT NOT NULL,
    purpose TEXT NOT NULL,
    state TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    extra_summary TEXT NOT NULL DEFAULT '',
    internal_note TEXT NOT NULL DEFAULT '',
    return_summary TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    contact_phone TEXT NOT NULL DEFAULT '',
    respond_by TEXT NOT NULL DEFAULT '',
    escalate_by TEXT NOT NULL DEFAULT '',
    reminders_sent INTEGER NOT NULL DEFAULT 0,
    token_version INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- 同一患者对同一机构、同一目的只允许一个进行中的转诊（终态自动释放）
CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_active_conflict
    ON referral(patient_ref, receiving_org, purpose)
    WHERE state IN ({_ACTIVE_LIST});
CREATE TABLE IF NOT EXISTS case_event (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_org TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    detail TEXT NOT NULL,
    op_id TEXT NOT NULL DEFAULT '',
    prev_hash TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS processed_op (
    op_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS access_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id TEXT NOT NULL,
    viewer_org TEXT NOT NULL,
    viewer_role TEXT NOT NULL,
    action TEXT NOT NULL,
    fields TEXT NOT NULL,
    via TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS link_token (
    token TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    token_version INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    notification_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    target_org TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_REFERRAL_COLUMNS = (
    "case_id", "patient_ref", "origin_org", "receiving_org", "purpose", "state",
    "summary", "extra_summary", "internal_note", "return_summary", "reject_reason",
    "contact_name", "contact_phone", "respond_by", "escalate_by", "reminders_sent",
    "token_version", "version", "created_at", "updated_at",
)


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.isolation_level = None  # 自动提交；事务由 transaction() 显式控制
        with self._lock:
            self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        """串行化的写事务：BEGIN IMMEDIATE 保证读写原子，重启安全。"""
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    # ------------------------------------------------------------ 基础登记（兼容既有能力）

    def save(self, record: Record) -> Record:
        value = record.with_timestamp()
        with self._lock:
            self.connection.execute(
                "INSERT INTO referral_case(record_id, owner_id, state, created_at) VALUES(?,?,?,?)",
                (value.record_id, value.owner_id, value.state, value.created_at),
            )
        return value

    def get(self, record_id: str) -> Record | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT record_id, owner_id, state, created_at FROM referral_case WHERE record_id=?",
                (record_id,),
            ).fetchone()
        return Record(**dict(row)) if row else None

    # ------------------------------------------------------------ 转诊单

    @staticmethod
    def _to_referral(row: sqlite3.Row) -> Referral:
        return Referral(**{col: row[col] for col in _REFERRAL_COLUMNS})

    def insert_referral(self, referral: Referral) -> None:
        values = tuple(getattr(referral, col) for col in _REFERRAL_COLUMNS)
        try:
            self.connection.execute(
                f"INSERT INTO referral({','.join(_REFERRAL_COLUMNS)}) "
                f"VALUES({','.join('?' * len(_REFERRAL_COLUMNS))})",
                values,
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateActive(
                f"患者 {referral.patient_ref} 在 {referral.receiving_org} "
                f"已有进行中的「{referral.purpose}」转诊"
            ) from exc

    def get_referral(self, case_id: str) -> Referral | None:
        with self._lock:
            row = self.connection.execute(
                f"SELECT {','.join(_REFERRAL_COLUMNS)} FROM referral WHERE case_id=?",
                (case_id,),
            ).fetchone()
        return self._to_referral(row) if row else None

    def update_referral(self, referral: Referral, expected_version: int) -> None:
        sets = ",".join(f"{col}=?" for col in _REFERRAL_COLUMNS if col != "case_id")
        values = tuple(getattr(referral, col) for col in _REFERRAL_COLUMNS if col != "case_id")
        cur = self.connection.execute(
            f"UPDATE referral SET {sets} WHERE case_id=? AND version=?",
            values + (referral.case_id, expected_version),
        )
        if cur.rowcount != 1:
            raise StateConflict(f"转诊单 {referral.case_id} 发生并发冲突，请重试")

    def find_active_conflict(self, patient_ref: str, receiving_org: str, purpose: str) -> str | None:
        with self._lock:
            row = self.connection.execute(
                f"SELECT case_id FROM referral WHERE patient_ref=? AND receiving_org=? "
                f"AND purpose=? AND state IN ({_ACTIVE_LIST}) LIMIT 1",
                (patient_ref, receiving_org, purpose),
            ).fetchone()
        return row["case_id"] if row else None

    def list_deadline_cases(self) -> list[Referral]:
        with self._lock:
            rows = self.connection.execute(
                f"SELECT {','.join(_REFERRAL_COLUMNS)} FROM referral "
                "WHERE state IN ('pending_accept','supplemented','awaiting_supplement') "
                "AND (respond_by != '' OR escalate_by != '') ORDER BY created_at"
            ).fetchall()
        return [self._to_referral(row) for row in rows]

    # ------------------------------------------------------------ 事件链

    def append_event(self, event: Event) -> Event:
        cur = self.connection.execute(
            "INSERT INTO case_event(case_id, event_type, actor_org, actor_role, detail, "
            "op_id, prev_hash, hash, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                event.case_id, event.event_type, event.actor_org, event.actor_role,
                json.dumps(event.detail, ensure_ascii=False, sort_keys=True),
                event.op_id, event.prev_hash, event.hash, event.created_at,
            ),
        )
        return Event(**{**event.__dict__, "seq": cur.lastrowid})

    def list_events(self, case_id: str) -> list[Event]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT seq, case_id, event_type, actor_org, actor_role, detail, op_id, "
                "prev_hash, hash, created_at FROM case_event WHERE case_id=? ORDER BY seq",
                (case_id,),
            ).fetchall()
        return [
            Event(
                seq=row["seq"], case_id=row["case_id"], event_type=row["event_type"],
                actor_org=row["actor_org"], actor_role=row["actor_role"],
                detail=json.loads(row["detail"]), op_id=row["op_id"],
                prev_hash=row["prev_hash"], hash=row["hash"], created_at=row["created_at"],
            )
            for row in rows
        ]

    def last_event_hash(self, case_id: str) -> str:
        with self._lock:
            row = self.connection.execute(
                "SELECT hash FROM case_event WHERE case_id=? ORDER BY seq DESC LIMIT 1",
                (case_id,),
            ).fetchone()
        return row["hash"] if row else ""

    # ------------------------------------------------------------ 幂等操作

    def get_op(self, op_id: str) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT op_id, case_id, action, result, created_at FROM processed_op WHERE op_id=?",
                (op_id,),
            ).fetchone()
        return dict(row) if row else None

    def put_op(self, op_id: str, case_id: str, action: str, result_json: str, created_at: str) -> None:
        self.connection.execute(
            "INSERT INTO processed_op(op_id, case_id, action, result, created_at) VALUES(?,?,?,?,?)",
            (op_id, case_id, action, result_json, created_at),
        )

    # ------------------------------------------------------------ 访问记录

    def add_access(self, case_id: str, viewer_org: str, viewer_role: str,
                   action: str, fields: tuple[str, ...], via: str, created_at: str) -> None:
        self.connection.execute(
            "INSERT INTO access_log(case_id, viewer_org, viewer_role, action, fields, via, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (case_id, viewer_org, viewer_role, action, json.dumps(sorted(fields), ensure_ascii=False),
             via, created_at),
        )

    def list_access(self, case_id: str) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT seq, case_id, viewer_org, viewer_role, action, fields, via, created_at "
                "FROM access_log WHERE case_id=? ORDER BY seq",
                (case_id,),
            ).fetchall()
        return [
            {**{k: row[k] for k in ("seq", "case_id", "viewer_org", "viewer_role", "action", "via", "created_at")},
             "fields": json.loads(row["fields"])}
            for row in rows
        ]

    # ------------------------------------------------------------ 链接令牌

    def insert_token(self, token: str, case_id: str, scope: str,
                     token_version: int, created_at: str) -> None:
        self.connection.execute(
            "INSERT INTO link_token(token, case_id, scope, token_version, revoked, created_at) "
            "VALUES(?,?,?,?,0,?)",
            (token, case_id, scope, token_version, created_at),
        )

    def get_token(self, token: str) -> dict | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT token, case_id, scope, token_version, revoked, created_at "
                "FROM link_token WHERE token=?",
                (token,),
            ).fetchone()
        return dict(row) if row else None

    def revoke_tokens(self, case_id: str) -> None:
        self.connection.execute("UPDATE link_token SET revoked=1 WHERE case_id=?", (case_id,))

    # ------------------------------------------------------------ 通知箱（网络重试可审计）

    def enqueue(self, notification_id: str, case_id: str, kind: str, target_org: str,
                payload: dict, created_at: str) -> None:
        self.connection.execute(
            "INSERT INTO outbox(notification_id, case_id, kind, target_org, payload, status, "
            "attempts, last_error, created_at, updated_at) VALUES(?,?,?,?,?,'pending',0,'',?,?)",
            (notification_id, case_id, kind, target_org,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), created_at, created_at),
        )

    def pending_notifications(self) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT notification_id, case_id, kind, target_org, payload, status, attempts, "
                "last_error, created_at, updated_at FROM outbox "
                "WHERE status='pending' ORDER BY created_at, notification_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_notification(self, notification_id: str, status: str, attempts: int,
                            last_error: str, updated_at: str) -> None:
        self.connection.execute(
            "UPDATE outbox SET status=?, attempts=?, last_error=?, updated_at=? "
            "WHERE notification_id=?",
            (status, attempts, last_error, updated_at, notification_id),
        )

    def list_notifications(self, case_id: str) -> list[dict]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT notification_id, case_id, kind, target_org, payload, status, attempts, "
                "last_error, created_at, updated_at FROM outbox WHERE case_id=? "
                "ORDER BY created_at, notification_id",
                (case_id,),
            ).fetchall()
        return [
            {**row_dict, "payload": json.loads(row_dict["payload"])}
            for row_dict in (dict(row) for row in rows)
        ]
