"""SQLite storage. Every public memory query takes a trusted adapter identity."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from qq_agent.messages import IncomingMessage


class MemoryAccessError(ValueError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 platform TEXT NOT NULL, user_id TEXT NOT NULL, nickname TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(platform,user_id));
CREATE TABLE IF NOT EXISTS groups (
 platform TEXT NOT NULL, group_id TEXT NOT NULL, created_at REAL NOT NULL,
 PRIMARY KEY(platform,group_id));
CREATE TABLE IF NOT EXISTS group_members (
 platform TEXT NOT NULL, group_id TEXT NOT NULL, user_id TEXT NOT NULL,
 first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL, consent_status TEXT NOT NULL,
 PRIMARY KEY(platform,group_id,user_id),
 FOREIGN KEY(platform,user_id) REFERENCES users(platform,user_id),
 FOREIGN KEY(platform,group_id) REFERENCES groups(platform,group_id));
CREATE TABLE IF NOT EXISTS sessions (
 thread_id TEXT PRIMARY KEY, platform TEXT NOT NULL, chat_type TEXT NOT NULL,
 chat_id TEXT NOT NULL, user_id TEXT NOT NULL, turns TEXT NOT NULL DEFAULT '[]',
 updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, platform TEXT NOT NULL, message_id TEXT NOT NULL,
 thread_id TEXT NOT NULL, user_id TEXT NOT NULL, chat_type TEXT NOT NULL,
 chat_id TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL,
 reply TEXT, UNIQUE(platform,chat_type,chat_id,user_id,message_id));
CREATE TABLE IF NOT EXISTS memories (
 id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, owner_id TEXT NOT NULL,
 chat_type TEXT NOT NULL, chat_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('personal','group')),
 scope TEXT NOT NULL CHECK(scope IN ('private','group','global')),
 category TEXT NOT NULL DEFAULT 'explicit', memory_key TEXT NOT NULL, content TEXT NOT NULL,
 source_type TEXT NOT NULL DEFAULT 'explicit', confidence REAL NOT NULL DEFAULT 1,
 sensitivity TEXT NOT NULL DEFAULT 'user_supplied', status TEXT NOT NULL DEFAULT 'active',
 created_at REAL NOT NULL, updated_at REAL NOT NULL, expires_at REAL,
 CHECK(scope != 'group' OR chat_type='group'), CHECK(kind != 'group' OR scope='group'),
 UNIQUE(platform,owner_id,chat_type,chat_id,memory_key));
CREATE TABLE IF NOT EXISTS message_receipts (
 thread_id TEXT NOT NULL, message_id TEXT NOT NULL, created_at REAL NOT NULL,
 reply TEXT, tombstone INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(thread_id,message_id));
CREATE TABLE IF NOT EXISTS consent_records (
 id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 actor_id TEXT NOT NULL, action TEXT NOT NULL, source_message_id TEXT NOT NULL,
 created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS memory_sources (
 id INTEGER PRIMARY KEY, memory_id INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
 message_row_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
 source_user_id TEXT NOT NULL, source_message_id TEXT NOT NULL,
 event TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS purge_queue (thread_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS group_context_messages (
 platform TEXT NOT NULL,chat_id TEXT NOT NULL,user_id TEXT NOT NULL,message_id TEXT NOT NULL,
 nickname TEXT,content TEXT NOT NULL,created_at REAL NOT NULL,tombstone INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(platform,chat_id,user_id,message_id));
CREATE INDEX IF NOT EXISTS group_context_lookup ON group_context_messages(platform,chat_id,created_at);
CREATE TRIGGER IF NOT EXISTS group_context_redact AFTER DELETE ON messages BEGIN
 UPDATE group_context_messages SET content='',nickname=NULL,tombstone=1
 WHERE platform=old.platform AND chat_id=old.chat_id AND user_id=old.user_id
 AND message_id=old.message_id AND old.chat_type='group'; END;
CREATE INDEX IF NOT EXISTS memories_visibility ON memories(platform,scope,chat_type,chat_id,owner_id);
CREATE INDEX IF NOT EXISTS messages_expiry ON messages(created_at);
CREATE INDEX IF NOT EXISTS sessions_owner ON sessions(platform,user_id);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content,tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
 INSERT INTO memories_fts(rowid,content) VALUES(new.id,new.content); END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
 DELETE FROM memories_fts WHERE rowid=old.id; END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content ON memories BEGIN
 DELETE FROM memories_fts WHERE rowid=old.id;
 INSERT INTO memories_fts(rowid,content) VALUES(new.id,new.content); END;
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(content,tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
 INSERT INTO messages_fts(rowid,content) VALUES(new.id,new.content); END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
 DELETE FROM messages_fts WHERE rowid=old.id; END;
PRAGMA user_version=2;
"""


class Database:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA secure_delete=ON")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            self.conn.close()
            raise ValueError("数据库版本较新，请升级程序")
        self.conn.executescript(SCHEMA)

    def close(self):
        self.conn.close()

    @staticmethod
    def _group_allowed(actor, groups, users):
        return (
            actor.platform == "qq"
            and actor.chat_type == "group"
            and actor.public_response
            and actor.chat_id in groups
            and (not users or actor.user_id in users)
        )

    def observe_group(self, actor, groups, users):
        if not self._group_allowed(actor, groups, users):
            return
        # Commands are only handled when addressing the bot; never turn ambient
        # personal-memory instructions into a shared memory or a model request.
        if actor.text.startswith(("/", "记住")):
            return
        with self.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO group_context_messages "
                "(platform,chat_id,user_id,message_id,nickname,content,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    actor.platform,
                    actor.chat_id,
                    actor.user_id,
                    actor.message_id,
                    actor.nickname,
                    actor.text,
                    time.time(),
                ),
            )

    def group_context(self, actor, groups, users, days, targets=()):
        if not self._group_allowed(actor, groups, users):
            return []
        query = (
            "SELECT user_id,message_id,nickname,content,created_at FROM group_context_messages "
            "WHERE platform=? AND chat_id=? AND tombstone=0 AND created_at>?"
        )
        args = [actor.platform, actor.chat_id, time.time() - days * 86400]
        for selected in (users, targets):
            if selected:
                query += " AND user_id IN (" + ",".join("?" for _ in selected) + ")"
                args.extend(selected)
        rows = self.conn.execute(query + " ORDER BY created_at DESC LIMIT 60", args).fetchall()
        result, size = [], 0
        for row in rows:
            item = dict(row)
            item["truncated"] = len(item["content"]) > 1200
            item["content"] = item["content"][:1200]
            cost = len(json.dumps(item, ensure_ascii=False))
            if size + cost > 10000:
                continue
            result.append(item)
            size += cost
        return list(reversed(result))

    @contextmanager
    def transaction(self):
        with self.conn:
            yield self.conn

    def touch(self, actor: IncomingMessage):
        now = time.time()
        with self.transaction() as db:
            db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?) ON CONFLICT(platform,user_id) "
                "DO UPDATE SET nickname=excluded.nickname,updated_at=excluded.updated_at",
                (actor.platform, actor.user_id, actor.nickname, now, now),
            )
            if actor.chat_type == "group":
                db.execute(
                    "INSERT OR IGNORE INTO groups VALUES(?,?,?)",
                    (actor.platform, actor.chat_id, now),
                )
                db.execute(
                    "INSERT INTO group_members VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(platform,group_id,user_id) DO UPDATE SET last_seen_at=?",
                    (actor.platform, actor.chat_id, actor.user_id, now, now, "observed", now),
                )
            db.execute(
                "INSERT OR IGNORE INTO sessions(thread_id,platform,chat_type,chat_id,"
                "user_id,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    actor.thread_id,
                    actor.platform,
                    actor.chat_type,
                    actor.chat_id,
                    actor.user_id,
                    now,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO messages(platform,message_id,thread_id,user_id,"
                "chat_type,chat_id,content,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    actor.platform,
                    actor.message_id,
                    actor.thread_id,
                    actor.user_id,
                    actor.chat_type,
                    actor.chat_id,
                    actor.text,
                    now,
                ),
            )
            db.execute(
                "INSERT OR IGNORE INTO message_receipts(thread_id,message_id,created_at) "
                "VALUES(?,?,?)",
                (actor.thread_id, actor.message_id, now),
            )

    def reply(self, actor):
        row = self.conn.execute(
            "SELECT reply,tombstone FROM message_receipts WHERE thread_id=? AND message_id=?",
            (actor.thread_id, actor.message_id),
        ).fetchone()
        if row and row[1]:
            return {
                "text": "该历史事件已处理，相关上下文已清理。",
                "status": "ok",
                "tool_calls": 0,
                "model_steps": 0,
            }
        return json.loads(row[0]) if row and row[0] else None

    def turns(self, actor):
        row = self.conn.execute(
            "SELECT turns FROM sessions WHERE thread_id=?", (actor.thread_id,)
        ).fetchone()
        return json.loads(row[0]) if row else []

    def search_messages(self, actor, query):
        if not query or len(query) > 256:
            raise ValueError("历史检索词须为 1–256 字符")
        where, args = "thread_id=?", [actor.thread_id]
        if len(query) >= 3:
            where += " AND id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)"
            args.append('"' + query.replace('"', '""') + '"')
        else:
            where += " AND instr(lower(content),lower(?))>0"
            args.append(query)
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT message_id,content,created_at FROM messages WHERE "
                + where
                + " ORDER BY created_at DESC LIMIT 20",
                args,
            )
        ]

    def complete(self, actor, turns, reply):
        with self.transaction() as db:
            db.execute(
                "UPDATE sessions SET turns=?,updated_at=? WHERE thread_id=?",
                (json.dumps(turns, ensure_ascii=False), time.time(), actor.thread_id),
            )
            db.execute(
                "UPDATE messages SET reply=? WHERE thread_id=? AND message_id=?",
                (json.dumps(reply, ensure_ascii=False), actor.thread_id, actor.message_id),
            )
            db.execute(
                "UPDATE message_receipts SET reply=?,tombstone=0 WHERE thread_id=? AND message_id=?",
                (json.dumps(reply, ensure_ascii=False), actor.thread_id, actor.message_id),
            )

    @staticmethod
    def _visibility(actor):
        # Private is bound to its original chat; global stays owner-only and platform-bound.
        return (
            (
                "m.platform=? AND m.status='active' AND (m.expires_at IS NULL OR m.expires_at>?) "
                "AND ((m.owner_id=? AND (m.scope='global' OR (m.scope='private' AND "
                "m.chat_type=? AND m.chat_id=?))) OR (m.scope='group' AND ?='group' "
                "AND m.chat_type='group' AND m.chat_id=? AND EXISTS(SELECT 1 FROM group_members gm "
                "WHERE gm.platform=m.platform AND gm.group_id=m.chat_id AND gm.user_id=?)))"
            ),
            [
                actor.platform,
                time.time(),
                actor.user_id,
                actor.chat_type,
                actor.chat_id,
                actor.chat_type,
                actor.chat_id,
                actor.user_id,
            ],
        )

    def visible(self, actor, query=None, limit=100):
        where, args = self._visibility(actor)
        if actor.public_response:
            where += " AND m.scope='group'"
        if query:
            if len(query) > 256:
                raise ValueError("检索词过长")
            if len(query) >= 3:
                where += " AND m.id IN (SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?)"
                args.append('"' + query.replace('"', '""') + '"')
            else:
                where += " AND instr(lower(m.content),lower(?))>0"
                args.append(query)
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT m.* FROM memories m WHERE "
                + where
                + " ORDER BY m.updated_at DESC, m.id DESC LIMIT ?",
                [*args, min(max(limit, 1), 100)],
            )
        ]

    def owned(self, actor, memory_id):
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id=? AND platform=? AND owner_id=? "
            "AND (scope='global' OR (chat_type=? AND chat_id=?))",
            (memory_id, actor.platform, actor.user_id, actor.chat_type, actor.chat_id),
        ).fetchone()
        if not row:
            raise MemoryAccessError("记忆不存在或你无权修改")
        if actor.public_response and row["scope"] != "group":
            raise MemoryAccessError("个人记忆请私聊管理")
        return dict(row)

    def _invalidate(self, db, memory):
        if memory["scope"] == "group":
            clause, args = (
                "platform=? AND chat_type='group' AND chat_id=?",
                (memory["platform"], memory["chat_id"]),
            )
        else:
            clause, args = "platform=? AND user_id=?", (memory["platform"], memory["owner_id"])
        db.execute(
            "INSERT OR IGNORE INTO purge_queue SELECT thread_id FROM sessions WHERE " + clause, args
        )
        db.execute("UPDATE sessions SET turns='[]' WHERE " + clause, args)
        # Cached answers may contain an old or revoked memory.
        db.execute(
            "UPDATE messages SET reply=NULL WHERE thread_id IN (SELECT thread_id FROM purge_queue)"
        )
        db.execute(
            "UPDATE message_receipts SET reply=NULL,tombstone=1 WHERE thread_id IN "
            "(SELECT thread_id FROM purge_queue)"
        )

    def _source(self, db, actor, memory_id, event):
        row = db.execute(
            "SELECT id FROM messages WHERE thread_id=? AND message_id=?",
            (actor.thread_id, actor.message_id),
        ).fetchone()
        db.execute(
            "INSERT INTO memory_sources(memory_id,message_row_id,source_user_id,"
            "source_message_id,event,created_at) VALUES(?,?,?,?,?,?)",
            (
                memory_id,
                row[0] if row else None,
                actor.user_id,
                actor.message_id,
                event,
                time.time(),
            ),
        )
        db.execute(
            "INSERT INTO consent_records(memory_id,actor_id,action,source_message_id,"
            "created_at) VALUES(?,?,?,?,?)",
            (memory_id, actor.user_id, event, actor.message_id, time.time()),
        )

    def save_memory(self, actor, key, content, scope="private", kind="personal", expires_at=None):
        if not key.strip() or len(key) > 128 or not content.strip() or len(content) > 4000:
            raise ValueError("记忆键须为 1–128 字符，内容须为 1–4000 字符")
        if scope not in {"private", "group", "global"} or kind not in {"personal", "group"}:
            raise ValueError("无效的记忆范围或类型")
        if (scope == "group" and actor.chat_type != "group") or (
            kind == "group" and scope != "group"
        ):
            raise ValueError("群共享记忆只能在群会话中创建")
        now = time.time()
        # A separate storage location avoids silently converting a private note to global.
        chat_type, chat_id = (
            ("global", "@global") if scope == "global" else (actor.chat_type, actor.chat_id)
        )
        with self.transaction() as db:
            old = db.execute(
                "SELECT * FROM memories WHERE platform=? AND owner_id=? AND "
                "chat_type=? AND chat_id=? AND memory_key=?",
                (actor.platform, actor.user_id, chat_type, chat_id, key),
            ).fetchone()
            if old:
                self._invalidate(db, old)
                db.execute(
                    "DELETE FROM messages WHERE id IN (SELECT message_row_id "
                    "FROM memory_sources WHERE memory_id=?)",
                    (old["id"],),
                )
            db.execute(
                "INSERT INTO memories(platform,owner_id,chat_type,chat_id,kind,scope,"
                "memory_key,content,created_at,updated_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(platform,owner_id,chat_type,chat_id,memory_key) DO UPDATE SET "
                "content=excluded.content,scope=excluded.scope,kind=excluded.kind,"
                "updated_at=excluded.updated_at,expires_at=excluded.expires_at",
                (
                    actor.platform,
                    actor.user_id,
                    chat_type,
                    chat_id,
                    kind,
                    scope,
                    key,
                    content.strip(),
                    now,
                    now,
                    expires_at,
                ),
            )
            row = db.execute(
                "SELECT * FROM memories WHERE platform=? AND owner_id=? AND "
                "chat_type=? AND chat_id=? AND memory_key=?",
                (actor.platform, actor.user_id, chat_type, chat_id, key),
            ).fetchone()
            self._invalidate(db, row)
            self._source(db, actor, row["id"], "remember_" + scope)
            return row["id"]

    def change_memory(self, actor, memory_id, action, content=None):
        memory = self.owned(actor, memory_id)
        with self.transaction() as db:
            self._invalidate(db, memory)
            if action == "delete":
                # Delete original source texts too, not just the memory's search index.
                db.execute(
                    "DELETE FROM messages WHERE id IN (SELECT message_row_id "
                    "FROM memory_sources WHERE memory_id=?)",
                    (memory_id,),
                )
                db.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            elif action in {"share", "unshare"}:
                if memory["scope"] == "global" or memory["chat_type"] != "group":
                    raise ValueError("只能切换当前群的个人记忆共享范围")
                if memory["kind"] != "personal":
                    raise ValueError("群组记忆请纠正或删除，不支持改为私有")
                scope = "group" if action == "share" else "private"
                db.execute(
                    "UPDATE memories SET scope=?,updated_at=? WHERE id=?",
                    (scope, time.time(), memory_id),
                )
                self._invalidate(db, {**memory, "scope": scope})
                self._source(db, actor, memory_id, action)
            elif action == "correct":
                if not content or not content.strip() or len(content) > 4000:
                    raise ValueError("纠正内容须为 1–4000 字符")
                # Purge previous correction/source text so it cannot contradict the new fact.
                db.execute(
                    "DELETE FROM messages WHERE id IN (SELECT message_row_id "
                    "FROM memory_sources WHERE memory_id=?)",
                    (memory_id,),
                )
                db.execute(
                    "UPDATE memories SET content=?,updated_at=? WHERE id=?",
                    (content.strip(), time.time(), memory_id),
                )
                self._source(db, actor, memory_id, action)
            else:
                raise ValueError("无效的记忆操作")

    def sources(self, actor, memory_id):
        self.owned(actor, memory_id)
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT source_user_id,source_message_id,event,created_at FROM memory_sources "
                "WHERE memory_id=? ORDER BY id",
                (memory_id,),
            )
        ]

    def reset(self, actor):
        with self.transaction() as db:
            if (
                actor.chat_type == "group"
                and db.execute(
                    "SELECT 1 FROM group_context_messages WHERE platform=? AND chat_id=? "
                    "AND user_id=? AND tombstone=0 LIMIT 1",
                    (actor.platform, actor.chat_id, actor.user_id),
                ).fetchone()
            ):
                db.execute(
                    "UPDATE group_context_messages SET content='',nickname=NULL,tombstone=1 "
                    "WHERE platform=? AND chat_id=? AND user_id=?",
                    (actor.platform, actor.chat_id, actor.user_id),
                )
                self._invalidate(
                    db, {"scope": "group", "platform": actor.platform, "chat_id": actor.chat_id}
                )
            db.execute("INSERT OR IGNORE INTO purge_queue VALUES(?)", (actor.thread_id,))
            db.execute("UPDATE sessions SET turns='[]' WHERE thread_id=?", (actor.thread_id,))
            db.execute("DELETE FROM messages WHERE thread_id=?", (actor.thread_id,))
            db.execute(
                "UPDATE message_receipts SET reply=NULL,tombstone=1 WHERE thread_id=?",
                (actor.thread_id,),
            )

    def cleanup(self, days, now=None):
        now = time.time() if now is None else now
        cutoff = now - days * 86400
        with self.transaction() as db:
            groups = db.execute(
                "SELECT DISTINCT platform,chat_id FROM group_context_messages "
                "WHERE created_at<=? AND tombstone=0",
                (cutoff,),
            ).fetchall()
            for group in groups:
                self._invalidate(db, {"scope": "group", **dict(group)})
            db.execute("DELETE FROM group_context_messages WHERE created_at<=?", (cutoff,))
            expired = db.execute("SELECT * FROM memories WHERE expires_at<=?", (now,)).fetchall()
            for memory in expired:
                self._invalidate(db, memory)
                db.execute("DELETE FROM memories WHERE id=?", (memory["id"],))
            # If a thread contains any expired raw message, remove its entire recent
            # context: old text can have been quoted by a newer assistant reply.
            db.execute(
                "INSERT OR IGNORE INTO purge_queue SELECT thread_id FROM messages "
                "WHERE created_at<=?",
                (cutoff,),
            )
            db.execute(
                "INSERT OR IGNORE INTO purge_queue SELECT thread_id FROM sessions "
                "WHERE updated_at<=?",
                (cutoff,),
            )
            db.execute(
                "UPDATE sessions SET turns='[]' WHERE thread_id IN "
                "(SELECT thread_id FROM purge_queue)"
            )
            db.execute(
                "UPDATE messages SET reply=NULL WHERE thread_id IN "
                "(SELECT thread_id FROM purge_queue)"
            )
            db.execute("DELETE FROM messages WHERE created_at<=?", (cutoff,))
            db.execute(
                "UPDATE message_receipts SET reply=NULL,tombstone=1 WHERE thread_id IN "
                "(SELECT thread_id FROM purge_queue)"
            )
            db.execute("DELETE FROM message_receipts WHERE created_at<=?", (cutoff,))


def note_key(content):
    return "note-" + hashlib.sha256(content.encode()).hexdigest()[:24]
