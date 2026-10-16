"""Staff side of the bot: tasks of the status spreadsheet, who may do what with them,
status changes from Telegram, acceptance by the approver and the RACI events.

One source of events: every minute the worker compares task statuses with the last ones
it saw (table task_states) and turns differences into events; a change made from the bot
is recorded and announced at once. While the server runs, the Apps Script stops sending
its own status messages (heartbeat through the bridge), so nobody gets a message twice.

Who gets what (the RACI table on the «Справочник» sheet):
  В работе  -> C (once per task)          Риск      -> A and C
  Готово    -> A (with Accept / Return)   Отменено  -> R and I
            -> I                          accepted / returned -> R
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import TaskState
from app.services.booking import now_utc
from app.services.status_sheet import CANCELLED, DONE, NEW, RISK, WORK, Backend, Book, GspreadBackend, Task, build_book
from app.services.team import Person, build_matcher, match_people

log = logging.getLogger(__name__)


@dataclass
class Change:
    task: Task
    prev: str
    new: str
    source: str  # "sheet" (someone edited the spreadsheet) | "bot"
    actor: list[Person] = field(default_factory=list)  # who did it from the bot
    actor_name: str = ""
    reason: str = ""


@dataclass
class Review:
    """The approver's decision on a finished task, made from the bot."""

    task: Task
    accepted: bool
    actor: list[Person]
    actor_name: str
    reason: str = ""


class Snapshot:
    """A read of the spreadsheet plus the matching of task cells to people."""

    def __init__(self, book: Book, at: float) -> None:
        self.book, self.at = book, at
        self.matcher = build_matcher(book.people)
        self._cache: dict[tuple[str, str], list[Person]] = {}

    @property
    def tasks(self) -> list[Task]:
        return self.book.tasks

    def _match(self, kind: str, task: Task, text: str) -> list[Person]:
        key = (kind, task.id)
        if key not in self._cache:
            self._cache[key] = match_people(text, task.product, self.matcher)[0]
        return self._cache[key]

    def r_of(self, t: Task) -> list[Person]:
        return self._match("r", t, t.r)

    def a_of(self, t: Task) -> list[Person]:
        return self._match("a", t, t.a or self.book.default_a.get(t.dept, ""))

    def c_of(self, t: Task) -> list[Person]:
        return self._match("c", t, t.c)

    def i_of(self, t: Task) -> list[Person]:
        return self._match("i", t, t.i)

    def staff(self, username: str | None, chat_id: int) -> list[Person]:
        """Rows of «Команда» for this Telegram user: by chat_id or by @username."""
        u = (username or "").lower()
        return [p for p in self.book.people if p.chat_id == str(chat_id) or (u and p.username == u)]

    def heads_of(self, people: list[Person]) -> list[str]:
        """Departments where one of these people is the default approver (from «Справочник»)."""
        keys = {p.key for p in people}
        depts = sorted({t.dept for t in self.tasks if t.dept})
        result = []
        for d in depts:
            text = self.book.default_a.get(d)
            if text and keys & {p.key for p in match_people(text, "", self.matcher)[0]}:
                result.append(d)
        return result


def _is(people: list[Person], among: list[Person]) -> bool:
    keys = {p.key for p in people}
    return any(p.key in keys for p in among)


def week_bounds(today: date) -> tuple[date, date]:
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=6)


class TaskBoard:
    def __init__(self) -> None:
        self.backend: Backend | None = None
        self.snapshot: Snapshot | None = None
        self.max_age = 60.0  # seconds a read stays fresh for the screens
        self.see_all: set[str] = set()  # roles (as on «Команда») that see every department
        self.test_chat_id: int | None = None  # the spreadsheet's test mode, learned through the bridge
        self.bridge = None  # app.services.bridge.Bridge: heartbeat to the Apps Script
        self.last_poll = float("-inf")
        self.last_heartbeat = float("-inf")
        self.last_error: str | None = None
        self.events_claimed = False  # the Apps Script was told the server sends status events
        self.lock = asyncio.Lock()  # one writer/poller at a time: a poll must not see half a change

    @property
    def enabled(self) -> bool:
        return self.backend is not None

    def configure(self, status_sheet_id: str | None, key_file: str | None, see_all: list[str]) -> None:
        import os

        if status_sheet_id and key_file and os.path.exists(key_file):
            self.backend = GspreadBackend(status_sheet_id, key_file)
        self.see_all = {r.strip().lower() for r in see_all if r.strip()}

    async def _read(self) -> Snapshot:
        sheets, team, ref = await self.backend.read()
        self.snapshot = Snapshot(build_book(sheets, team, ref, self.backend.url), time.monotonic())
        return self.snapshot

    async def get(self, *, fresh: bool = False) -> Snapshot:
        if fresh or self.snapshot is None or time.monotonic() - self.snapshot.at > self.max_age:
            return await self._read()
        return self.snapshot

    # --- queries ---------------------------------------------------------------------

    @staticmethod
    def my_tasks(snap: Snapshot, people: list[Person], today: date) -> list[Task]:
        """Open tasks where they are the executor: overdue, due up to the end of next week,
        or already in work / at risk. Soonest first."""
        _, week_end = week_bounds(today)
        horizon = week_end + timedelta(days=7)
        out = [
            t for t in snap.tasks
            if t.is_open and _is(people, snap.r_of(t)) and (t.status in (WORK, RISK) or (t.due and t.due <= horizon))
        ]
        return sorted(out, key=lambda t: (t.due or date.max, t.id))

    @staticmethod
    def to_review(snap: Snapshot, people: list[Person], accepted: set[str]) -> list[Task]:
        """Finished tasks waiting for these people's acceptance (not their own work)."""
        out = [
            t for t in snap.tasks
            if t.status == DONE and t.id not in accepted and _is(people, snap.a_of(t)) and not _is(people, snap.r_of(t))
        ]
        return sorted(out, key=lambda t: (t.fact or t.due or date.max, t.id))

    def visible_depts(self, snap: Snapshot, people: list[Person], is_admin: bool) -> list[str]:
        all_depts = sorted({t.dept for t in snap.tasks if t.dept})
        if is_admin or any(p.role.lower() in self.see_all for p in people):
            return all_depts
        return snap.heads_of(people)

    def rights(self, snap: Snapshot, t: Task, people: list[Person], is_admin: bool) -> set[str]:
        """What these people may do with the task from the bot."""
        r, a = _is(people, snap.r_of(t)), _is(people, snap.a_of(t)) or is_admin
        any_role = r or a or _is(people, snap.c_of(t)) or _is(people, snap.i_of(t))
        rights = set()
        if t.status == CANCELLED:
            return rights
        if r and t.status in (NEW, RISK):
            rights.add("work")
        if r and t.status in (NEW, WORK, RISK):
            rights.add("done")
        if any_role and t.status in (NEW, WORK):
            rights.add("risk")
        if a and t.status == DONE:
            rights |= {"accept", "return"}
        if a and t.status != DONE:
            rights.add("cancel")
        return rights

    async def accepted_ids(self, session: AsyncSession) -> set[str]:
        rows = await session.execute(select(TaskState.task_id).where(TaskState.accepted_at.is_not(None)))
        return set(rows.scalars())

    async def state(self, session: AsyncSession, task_id: str) -> TaskState | None:
        return await session.get(TaskState, task_id)

    # --- changes from the bot -----------------------------------------------------------

    async def change(
        self, session: AsyncSession, *, task_id: str, new: str, actor: list[Person], actor_name: str, reason: str = "", today: date
    ) -> Change | None:
        """Write a new status (and the fact date / comment that go with it) to the sheet."""
        async with self.lock:
            snap = await self.get(fresh=True)
            t = snap.book.by_id.get(task_id)
            if t is None or t.status == new:
                return None
            values: dict[str, object] = {"status": new}
            if new == DONE and not t.fact:
                values["fact"] = today.isoformat()
            if t.status == DONE and new != DONE:
                values["fact"] = ""  # returned: the work is not finished after all
            if reason:
                note = f"{today:%d.%m} {actor_name}: {reason}"
                values["comment"] = f"{t.comment}\n{note}" if t.comment else note
            updated = await self.backend.write(t, values)
            if updated is None:
                return None
            prev = t.status
            snap.book.tasks[snap.book.tasks.index(t)] = updated
            snap.book.by_id[task_id] = updated
            await self._record(session, updated, prev)
            await session.commit()
            return Change(updated, prev, new, "bot", actor, actor_name, reason)

    async def accept(self, session: AsyncSession, *, task_id: str, actor: list[Person], actor_name: str, tg_id: int) -> Review | None:
        async with self.lock:
            snap = await self.get()
            t = snap.book.by_id.get(task_id)
            if t is None or t.status != DONE:
                return None
            st = await session.get(TaskState, task_id) or TaskState(task_id=task_id, status=t.status)
            if st.accepted_at is not None:
                return None
            st.accepted_at, st.accepted_by, st.accepted_by_tg_id = now_utc(), actor_name[:128], tg_id
            session.add(st)
            await session.commit()
            return Review(t, True, actor, actor_name)

    # --- polling the sheet -------------------------------------------------------------

    async def _record(self, session: AsyncSession, t: Task, prev: str | None) -> TaskState:
        st = await session.get(TaskState, t.id)
        if st is None:
            st = TaskState(task_id=t.id, status=t.status, changed_at=now_utc())
            session.add(st)
        elif st.status != t.status:
            st.status, st.changed_at = t.status, now_utc()
        if t.status != DONE:
            st.accepted_at = st.accepted_by = st.accepted_by_tg_id = None
        return st

    async def poll(self, session: AsyncSession) -> list[Change]:
        """Statuses changed in the spreadsheet since the last look. The first look only remembers."""
        async with self.lock:
            snap = await self.get(fresh=True)
            states = {s.task_id: s for s in (await session.execute(select(TaskState))).scalars()}
            changes = []
            for t in snap.tasks:
                st = states.get(t.id)
                if st is None:
                    states[t.id] = TaskState(task_id=t.id, status=t.status, changed_at=now_utc())
                    session.add(states[t.id])  # kept in `states`: a duplicated ID is not added twice
                elif st.status != t.status:
                    changes.append(Change(t, st.status, t.status, "sheet"))
                    await self._record(session, t, st.status)
            await session.commit()
            return changes

    async def mark_started_notified(self, session: AsyncSession, task_id: str) -> bool:
        """True the first time only: C hears about a task's start once."""
        st = await session.get(TaskState, task_id)
        if st is None or st.c_notified:
            return False
        st.c_notified = True
        await session.commit()
        return True


def today_in(tz) -> date:
    return datetime.now(tz).date()


board = TaskBoard()
