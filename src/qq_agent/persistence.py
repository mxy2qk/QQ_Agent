import asyncio
import os
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from qq_agent.storage import Database


class Persistence:
    def __init__(self, database, checkpointer, days):
        self.database = database
        self.checkpointer = checkpointer
        self.days = days
        # Small local deployment: serialize persisted operations so a revocation
        # cannot race with another member generating/storing an old shared answer.
        self.lock = asyncio.Lock()

    async def flush_purges(self):
        rows = self.database.conn.execute("SELECT thread_id FROM purge_queue").fetchall()
        for row in rows:
            await self.checkpointer.adelete_thread(row[0])
            with self.database.transaction() as db:
                db.execute("DELETE FROM purge_queue WHERE thread_id=?", (row[0],))
        return {r[0] for r in rows}

    async def cleanup(self, now=None):
        self.database.cleanup(self.days, now)
        return await self.flush_purges()


@asynccontextmanager
async def open_persistence(settings):
    db_path = Path(settings.database_path).resolve()
    cp_path = Path(settings.checkpoint_db_path).resolve()
    if db_path == cp_path:
        raise ValueError("业务数据库和 Checkpoint 数据库必须使用不同文件")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cp_path.parent.mkdir(parents=True, exist_ok=True)
    # OS advisory locks are released automatically after a crash. Acquire both
    # paths so independent CLI processes cannot bypass the in-process privacy lock.
    locks = []
    database = None
    try:
        for path in sorted({str(db_path), str(cp_path)}):
            handle = open(path + ".lock", "a+b")  # noqa: ASYNC230, SIM115 -- owned until finally
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                raise ValueError("数据库正在被另一个 Agent 进程使用，请先关闭该进程") from None
            locks.append(handle)
        database = Database(db_path)
        async with AsyncExitStack() as stack:
            saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(str(cp_path)))
            await saver.setup()
            await saver.conn.execute("PRAGMA secure_delete=ON")
            state = Persistence(database, saver, settings.message_retention_days)
            await state.cleanup()
            yield state
    finally:
        if database:
            database.close()
        for handle in reversed(locks):
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
