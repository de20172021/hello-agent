"""SQLite 存储层：会话、消息、空间与定时任务。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any

from .settings import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL DEFAULT '新对话',
    model_id       TEXT,
    system_prompt  TEXT,
    temperature    REAL,
    top_p          REAL,
    max_tokens     INTEGER,
    tools_enabled  INTEGER DEFAULT 1,
    thinking       INTEGER DEFAULT 0,
    thinking_level TEXT DEFAULT 'medium',
    active_skills  TEXT,
    summary        TEXT,
    summary_until  INTEGER DEFAULT 0,
    compacted_at   REAL,
    pinned         INTEGER DEFAULT 0,
    space_id       TEXT,
    output_dir     TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS spaces (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    path           TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT 'created',
    kind           TEXT DEFAULT 'space',
    icon           TEXT DEFAULT '',
    description    TEXT DEFAULT '',
    instructions   TEXT DEFAULT '',
    pinned         INTEGER DEFAULT 0,
    archived       INTEGER DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL,
    last_opened_at REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content         TEXT DEFAULT '',
    reasoning       TEXT DEFAULT '',
    tool_calls      TEXT,
    tool_call_id    TEXT,
    name            TEXT,
    stats           TEXT,
    attachments     TEXT,
    created_at      REAL NOT NULL,
    seq             INTEGER NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, seq);

CREATE TABLE IF NOT EXISTS schedules (
    id              TEXT PRIMARY KEY,
    title           TEXT DEFAULT '',
    prompt          TEXT NOT NULL,
    conversation_id TEXT,
    space_id        TEXT,
    model_id        TEXT,
    kind            TEXT NOT NULL DEFAULT 'daily',
    at              REAL,
    every_minutes   INTEGER,
    time_of_day     TEXT,
    weekday         INTEGER,
    enabled         INTEGER DEFAULT 1,
    next_run_at     REAL,
    last_run_at     REAL,
    last_run_id     TEXT,
    last_status     TEXT,
    run_count       INTEGER DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
"""

# 老库升级：表里缺的列在启动时补齐
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "conversations": [
        ("thinking", "INTEGER DEFAULT 0"),
        ("thinking_level", "TEXT DEFAULT 'medium'"),
        ("active_skills", "TEXT"),
        ("summary", "TEXT"),
        ("summary_until", "INTEGER DEFAULT 0"),
        ("compacted_at", "REAL"),
        ("pinned", "INTEGER DEFAULT 0"),
        # 归属的空间；NULL = 任务（直接跑在项目目录）
        ("space_id", "TEXT"),
        # 本会话的产物目录（相对工作目录），新建文件默认写这里
        ("output_dir", "TEXT"),
    ],
    "spaces": [
        # space = 普通空间；schedule = 定时任务的专属目录（不在侧边栏列出）
        ("kind", "TEXT DEFAULT 'space'"),
    ],
    "schedules": [
        # 每个定时任务对应一个目录（空间），执行产生的对话都挂在这里
        ("space_id", "TEXT"),
    ],
    "messages": [
        ("reasoning", "TEXT DEFAULT ''"),
        # 多模态附件（图片/文档）的元数据，JSON 数组
        ("attachments", "TEXT"),
    ],
}


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:16]
    return f"{prefix}{raw}" if prefix else raw


class Store:
    def __init__(self, path=DB_PATH):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self._lock:
            for table, columns in MIGRATIONS.items():
                existing = {
                    row["name"]
                    for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, ddl in columns:
                    if name not in existing:
                        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_space ON conversations(space_id)"
            )
            self._conn.commit()

    # ---------------- 会话 ----------------

    def list_conversations(
        self, search: str | None = None, scope: str | None = None
    ) -> list[dict[str, Any]]:
        """scope: None=全部；"task"=只要任务；其它值按空间 id 过滤。"""
        with self._lock:
            sql = """
                SELECT c.*, s.name AS space_name, s.icon AS space_icon,
                       (SELECT COUNT(*) FROM messages m WHERE m.conversation_id = c.id) AS message_count,
                       (SELECT substr(m.content, 1, 120) FROM messages m
                         WHERE m.conversation_id = c.id AND m.role = 'user'
                         ORDER BY m.seq ASC LIMIT 1) AS preview
                FROM conversations c
                LEFT JOIN spaces s ON s.id = c.space_id
            """
            where: list[str] = []
            params: list[Any] = []
            if search:
                where.append(
                    "(c.title LIKE ? OR EXISTS (SELECT 1 FROM messages m2"
                    " WHERE m2.conversation_id = c.id AND m2.content LIKE ?))"
                )
                params.extend([f"%{search}%", f"%{search}%"])
            if scope == "task":
                where.append("c.space_id IS NULL")
            elif scope:
                where.append("c.space_id = ?")
                params.append(scope)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY c.pinned DESC, c.updated_at DESC"
            rows = self._conn.execute(sql, tuple(params)).fetchall()
            return [self._decode_conversation(dict(r)) for r in rows]

    def get_conversation(self, conv_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            return self._decode_conversation(dict(row)) if row else None

    def create_conversation(
        self,
        *,
        title: str = "新对话",
        model_id: str | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        tools_enabled: bool = True,
        thinking: bool = False,
        thinking_level: str = "medium",
        space_id: str | None = None,
        output_dir: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        conv_id = new_id("c_")
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversations
                   (id, title, model_id, system_prompt, temperature, top_p, max_tokens,
                    tools_enabled, thinking, thinking_level, space_id, output_dir,
                    created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    conv_id,
                    title,
                    model_id,
                    system_prompt,
                    temperature,
                    top_p,
                    max_tokens,
                    1 if tools_enabled else 0,
                    1 if thinking else 0,
                    thinking_level,
                    space_id,
                    output_dir,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_conversation(conv_id)  # type: ignore[return-value]

    def update_conversation(self, conv_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "title",
            "model_id",
            "system_prompt",
            "temperature",
            "top_p",
            "max_tokens",
            "tools_enabled",
            "thinking",
            "thinking_level",
            "active_skills",
            "summary",
            "summary_until",
            "compacted_at",
            "pinned",
            "space_id",
            "output_dir",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        for flag in ("tools_enabled", "thinking", "pinned"):
            if flag in updates:
                updates[flag] = 1 if updates[flag] else 0
        if "space_id" in updates and not updates["space_id"]:
            updates["space_id"] = None
        if "active_skills" in updates:
            value = updates["active_skills"]
            updates["active_skills"] = json.dumps(value or [], ensure_ascii=False)
        with self._lock:
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                self._conn.execute(
                    f"UPDATE conversations SET {sets}, updated_at = ? WHERE id = ?",
                    (*updates.values(), time.time(), conv_id),
                )
            else:
                self._conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (time.time(), conv_id),
                )
            self._conn.commit()
        return self.get_conversation(conv_id)

    def set_summary(self, conv_id: str, summary: str, until_seq: int) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE conversations
                   SET summary = ?, summary_until = ?, compacted_at = ?, updated_at = ?
                   WHERE id = ?""",
                (summary, int(until_seq), time.time(), time.time(), conv_id),
            )
            self._conn.commit()

    def touch_conversation(self, conv_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (time.time(), conv_id)
            )
            self._conn.commit()

    def delete_conversation(self, conv_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
            self._conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            self._conn.commit()

    # ---------------- 消息 ----------------

    def list_messages(self, conv_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY seq ASC", (conv_id,)
            ).fetchall()
        return [self._decode(r) for r in rows]

    def get_message(self, msg_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)).fetchone()
        return self._decode(row) if row else None

    def max_seq(self, conv_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
        return int(row["s"]) if row else 0

    def add_message(
        self,
        conv_id: str,
        role: str,
        *,
        content: str = "",
        reasoning: str = "",
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
        stats: dict | None = None,
        attachments: list | None = None,
    ) -> dict[str, Any]:
        msg_id = new_id("m_")
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM messages WHERE conversation_id = ?",
                (conv_id,),
            ).fetchone()
            seq = int(row["s"]) + 1
            self._conn.execute(
                """INSERT INTO messages
                   (id, conversation_id, role, content, reasoning, tool_calls,
                    tool_call_id, name, stats, attachments, created_at, seq)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    msg_id,
                    conv_id,
                    role,
                    content,
                    reasoning,
                    json.dumps(tool_calls, ensure_ascii=False) if tool_calls else None,
                    tool_call_id,
                    name,
                    json.dumps(stats, ensure_ascii=False) if stats else None,
                    json.dumps(attachments, ensure_ascii=False) if attachments else None,
                    now,
                    seq,
                ),
            )
            self._conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id)
            )
            self._conn.commit()
        return self.get_message(msg_id)  # type: ignore[return-value]

    def update_message(
        self,
        msg_id: str,
        *,
        content: str | None = None,
        reasoning: str | None = None,
        tool_calls: list | None = None,
        stats: dict | None = None,
    ) -> None:
        sets: list[str] = []
        values: list[Any] = []
        if content is not None:
            sets.append("content = ?")
            values.append(content)
        if reasoning is not None:
            sets.append("reasoning = ?")
            values.append(reasoning)
        if tool_calls is not None:
            sets.append("tool_calls = ?")
            values.append(json.dumps(tool_calls, ensure_ascii=False))
        if stats is not None:
            sets.append("stats = ?")
            values.append(json.dumps(stats, ensure_ascii=False))
        if not sets:
            return
        with self._lock:
            self._conn.execute(
                f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", (*values, msg_id)
            )
            self._conn.commit()

    def delete_message(self, msg_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
            self._conn.commit()

    def truncate_from(self, conv_id: str, msg_id: str) -> int:
        """删除某条消息及其之后的所有消息（用于「重新生成」）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT seq FROM messages WHERE id = ? AND conversation_id = ?",
                (msg_id, conv_id),
            ).fetchone()
            if not row:
                return 0
            cur = self._conn.execute(
                "DELETE FROM messages WHERE conversation_id = ? AND seq >= ?",
                (conv_id, row["seq"]),
            )
            self._conn.commit()
            return cur.rowcount

    # ---------------- 空间 ----------------

    SPACE_FIELDS = {
        "name",
        "path",
        "mode",
        "kind",
        "icon",
        "description",
        "instructions",
        "pinned",
        "archived",
        "last_opened_at",
    }

    def list_spaces(
        self, include_archived: bool = False, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """kind: None=全部；"space"=普通空间；"schedule"=定时任务目录。"""
        sql = """
            SELECT s.*,
                   (SELECT COUNT(*) FROM conversations c WHERE c.space_id = s.id) AS conversation_count
            FROM spaces s
        """
        where: list[str] = []
        params: list[Any] = []
        if not include_archived:
            where.append("s.archived = 0")
        if kind:
            where.append("COALESCE(s.kind, 'space') = ?")
            params.append(kind)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY s.pinned DESC, s.updated_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [self._decode_space(dict(r)) for r in rows]

    def get_space(self, space_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM spaces WHERE id = ?", (space_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            count = self._conn.execute(
                "SELECT COUNT(*) AS n FROM conversations WHERE space_id = ?", (space_id,)
            ).fetchone()
            item["conversation_count"] = int(count["n"]) if count else 0
        return self._decode_space(item)

    def add_space(self, **fields: Any) -> dict[str, Any]:
        now = time.time()
        space_id = fields.pop("id", None) or new_id("sp_")
        payload = {k: v for k, v in fields.items() if k in self.SPACE_FIELDS}
        payload.setdefault("name", "新空间")
        payload.setdefault("path", "")
        payload.setdefault("mode", "created")
        payload["pinned"] = 1 if payload.get("pinned") else 0
        payload["archived"] = 1 if payload.get("archived") else 0
        columns = ["id", *payload.keys(), "created_at", "updated_at"]
        values = [space_id, *payload.values(), now, now]
        placeholders = ",".join("?" for _ in columns)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO spaces ({','.join(columns)}) VALUES ({placeholders})", values
            )
            self._conn.commit()
        return self.get_space(space_id)  # type: ignore[return-value]

    def update_space(self, space_id: str, **fields: Any) -> dict[str, Any] | None:
        updates = {k: v for k, v in fields.items() if k in self.SPACE_FIELDS}
        for flag in ("pinned", "archived"):
            if flag in updates:
                updates[flag] = 1 if updates[flag] else 0
        with self._lock:
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                self._conn.execute(
                    f"UPDATE spaces SET {sets}, updated_at = ? WHERE id = ?",
                    (*updates.values(), time.time(), space_id),
                )
            else:
                self._conn.execute(
                    "UPDATE spaces SET updated_at = ? WHERE id = ?", (time.time(), space_id)
                )
            self._conn.commit()
        return self.get_space(space_id)

    def delete_space(self, space_id: str, delete_conversations: bool = False) -> dict[str, Any]:
        """移除空间。默认把里面的对话转成任务（保留内容），否则连对话一起删。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM conversations WHERE space_id = ?", (space_id,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if delete_conversations:
                for conv_id in ids:
                    self._conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conv_id,))
                    self._conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            else:
                self._conn.execute(
                    "UPDATE conversations SET space_id = NULL WHERE space_id = ?", (space_id,)
                )
            self._conn.execute("DELETE FROM spaces WHERE id = ?", (space_id,))
            self._conn.commit()
        return {"ok": True, "detached_conversations": 0 if delete_conversations else len(ids)}

    def assign_conversations_to_space(self, space_id: str, *, only_unassigned: bool = True) -> int:
        with self._lock:
            if only_unassigned:
                cur = self._conn.execute(
                    "UPDATE conversations SET space_id = ? WHERE space_id IS NULL", (space_id,)
                )
            else:
                cur = self._conn.execute("UPDATE conversations SET space_id = ?", (space_id,))
            self._conn.commit()
            return cur.rowcount

    @staticmethod
    def _decode_space(item: dict[str, Any]) -> dict[str, Any]:
        item["pinned"] = bool(item.get("pinned"))
        item["archived"] = bool(item.get("archived"))
        item["kind"] = item.get("kind") or "space"
        item.setdefault("conversation_count", 0)
        return item

    # ---------------- 定时任务 ----------------

    SCHEDULE_FIELDS = {
        "title",
        "prompt",
        "conversation_id",
        "space_id",
        "model_id",
        "kind",
        "at",
        "every_minutes",
        "time_of_day",
        "weekday",
        "enabled",
        "next_run_at",
        "last_run_at",
        "last_run_id",
        "last_status",
        "run_count",
    }

    def list_schedules(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM schedules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY COALESCE(next_run_at, 0) ASC, created_at ASC"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [self._decode_schedule(dict(r)) for r in rows]

    def get_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
            ).fetchone()
        return self._decode_schedule(dict(row)) if row else None

    def add_schedule(self, **fields: Any) -> dict[str, Any]:
        now = time.time()
        schedule_id = fields.pop("id", None) or new_id("s_")
        payload = {k: v for k, v in fields.items() if k in self.SCHEDULE_FIELDS}
        payload.setdefault("title", "")
        payload.setdefault("kind", "daily")
        payload.setdefault("enabled", True)
        payload["enabled"] = 1 if payload.get("enabled") else 0
        columns = ["id", *payload.keys(), "created_at", "updated_at"]
        values = [schedule_id, *payload.values(), now, now]
        placeholders = ",".join("?" for _ in columns)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO schedules ({','.join(columns)}) VALUES ({placeholders})", values
            )
            self._conn.commit()
        return self.get_schedule(schedule_id)  # type: ignore[return-value]

    def update_schedule(self, schedule_id: str, **fields: Any) -> dict[str, Any] | None:
        updates = {k: v for k, v in fields.items() if k in self.SCHEDULE_FIELDS}
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        with self._lock:
            if updates:
                sets = ", ".join(f"{k} = ?" for k in updates)
                self._conn.execute(
                    f"UPDATE schedules SET {sets}, updated_at = ? WHERE id = ?",
                    (*updates.values(), time.time(), schedule_id),
                )
            else:
                self._conn.execute(
                    "UPDATE schedules SET updated_at = ? WHERE id = ?",
                    (time.time(), schedule_id),
                )
            self._conn.commit()
        return self.get_schedule(schedule_id)

    def delete_schedule(self, schedule_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            self._conn.commit()

    @staticmethod
    def _decode_schedule(item: dict[str, Any]) -> dict[str, Any]:
        item["enabled"] = bool(item.get("enabled", 1))
        return item

    @staticmethod
    def _decode_conversation(item: dict[str, Any]) -> dict[str, Any]:
        raw = item.get("active_skills")
        if raw:
            try:
                item["active_skills"] = json.loads(raw)
            except json.JSONDecodeError:
                item["active_skills"] = []
        else:
            item["active_skills"] = []
        item["thinking"] = bool(item.get("thinking"))
        item["tools_enabled"] = bool(item.get("tools_enabled", 1))
        item["pinned"] = bool(item.get("pinned"))
        item.setdefault("space_id", None)
        return item

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for key in ("tool_calls", "stats", "attachments"):
            raw = item.get(key)
            if raw:
                try:
                    item[key] = json.loads(raw)
                except json.JSONDecodeError:
                    item[key] = None
            else:
                item[key] = None
        return item


store = Store()
