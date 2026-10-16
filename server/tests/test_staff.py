"""Staff tasks: the status spreadsheet (an in-memory fake), status buttons, acceptance,
reasons, RACI events, polling for manual edits, test mode, rights and the heads' summary."""

import copy
from datetime import date, timedelta

import pytest
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import select

from app.config import Settings
from app.db.models import TaskState
from app.services.status_sheet import COL, SERIAL_EPOCH, TEAM_COL, SheetValues, header_index, parse_tasks
from app.services.tasks import board, today_in
from tests.test_e2e_flow import Harness, h  # noqa: F401 - the fixture is used by name

BOBUR, LOLA, DILNOZA, TEMUR, CLIENT = 601, 602, 603, 604, 650
TZ = Settings(bot_token="1:a", _env_file=None).tz
TODAY = today_in(TZ)
HEAD = [COL[k] for k in ("id", "task", "r", "status", "start", "due", "fact", "comment", "week", "dept", "product", "result", "hours", "a", "c", "i")]


def serial(d: date) -> int:
    return (d - SERIAL_EPOCH).days


def row(task_id, task, r, status, due, *, start=None, fact=None, comment="", a="", c="", i="", hours=4):
    return [task_id, task, r, status, serial(start) if start else "", serial(due), serial(fact) if fact else "", comment,
            "Н1", "Маркетинг", "BMC", f"Критерий {task_id}", hours, a, c, i]


class FakeBackend:
    url = "https://docs.google.com/spreadsheets/d/TEST/edit"

    def __init__(self) -> None:
        marketing = [
            HEAD,
            row("MKT-001", "Сценарии роликов", "Маркетолог", "Не начато", TODAY + timedelta(days=3), start=TODAY, c="Дизайнер: обложки", i="Темур"),
            row("MKT-002", "Медиаплан", "Маркетолог", "В работе", TODAY - timedelta(days=2)),
            row("MKT-003", "Обложки", "Дизайнер", "Готово", TODAY, fact=TODAY),
        ]
        self.sheets = {
            103: SheetValues("Маркетинг", 103, marketing),
            100: SheetValues("Все задачи", 100, copy.deepcopy(marketing)),  # formula-built: must be skipped
        }
        team_head = [TEAM_COL[k] for k in ("role", "name", "dept", "aliases", "username", "chat_id", "notify", "generic", "products")]
        self.team = [
            team_head,
            ["Руководитель маркетинга (Бобур)", "Бобур", "Маркетинг", "руководитель маркетинга; бобура", f"@user{BOBUR}", BOBUR, "Да"],
            ["Маркетолог", "Лола", "Маркетинг", "", f"@user{LOLA}", LOLA, "Да"],
            ["Дизайнер", "Дильноза", "Маркетинг", "", f"@user{DILNOZA}", DILNOZA, "Да"],
            ["Темур", "Темур", "Администрация", "темура", f"@user{TEMUR}", TEMUR, "Да"],
        ]
        self.ref = [["Код в ID", "Отдел", "Утверждает по умолчанию (A)"], ["MKT", "Маркетинг", "Руководитель маркетинга"]]

    def cell(self, task_id: str, key: str):
        sh = self.sheets[103]
        idx = header_index(sh.values[0], COL)
        r = next(r for r in sh.values[1:] if r[idx["id"]] == task_id)
        return r[idx[key]]

    def set(self, task_id: str, key: str, value) -> None:
        sh = self.sheets[103]
        idx = header_index(sh.values[0], COL)
        next(r for r in sh.values[1:] if r[idx["id"]] == task_id)[idx[key]] = value

    async def read(self):
        return copy.deepcopy(list(self.sheets.values())), copy.deepcopy(self.team), copy.deepcopy(self.ref)

    async def write(self, task, values):
        sh = self.sheets[task.gid]
        idx = header_index(sh.values[0], COL)
        n = next(n for n, r in enumerate(sh.values) if n and r[idx["id"]] == task.id)
        for key, value in values.items():
            if key == "fact" and value:
                value = serial(date.fromisoformat(value))  # USER_ENTERED turns the ISO date into a date
            sh.values[n][idx[key]] = value
        updated = parse_tasks(SheetValues(sh.title, sh.gid, [sh.values[0], sh.values[n]]))[0]
        updated.row = n + 1
        return updated


@pytest.fixture
async def sheet(factory):
    fake = FakeBackend()
    board.backend, board.snapshot, board.test_chat_id = fake, None, None
    board.see_all = {"темур"}
    async with factory() as s:
        assert await board.poll(s) == []  # the first look only remembers statuses
    yield fake
    board.backend = board.snapshot = None
    board.see_all, board.test_chat_id = set(), None


def texts(h: Harness, chat_id: int) -> list[str]:
    return [m.text for m in h.api.sent_to(chat_id)]


def edits(h: Harness) -> list[str]:
    return [c.text for c in h.api.calls if isinstance(c, EditMessageText)]


def buttons(h: Harness, chat_id: int) -> list[str]:
    return [b.callback_data for r in h.api.last_markup(chat_id).inline_keyboard for b in r if b.callback_data]


async def start(h: Harness, uid: int) -> None:
    await h.send(uid, "/start")
    await h.press(uid, "lg:ru")


async def test_executor_done_and_approver_accepts(h: Harness, sheet: FakeBackend, factory):
    await start(h, LOLA)
    menu = [m.reply_markup for m in h.api.sent_to(LOLA) if isinstance(m.reply_markup, ReplyKeyboardMarkup)][-1]
    assert menu.keyboard[0][0].text == "🗂 Задачи"  # staff get the Tasks button

    await h.send(LOLA, "🗂 Задачи")
    home = h.api.sent_to(LOLA)[-1].text
    assert "Мои открытые: 2" in home and "просрочено: 1" in home
    await h.press(LOLA, "tl:mine:0")
    listing = [c for c in h.api.calls if isinstance(c, EditMessageText)][-1]
    labels = [b.text for r in listing.reply_markup.inline_keyboard for b in r]
    assert "Мои задачи</b> (2)" in listing.text
    assert labels[0].startswith("❗") and "MKT-002" in labels[0] and "MKT-001" in labels[1]  # overdue first
    await h.press(LOLA, "tk:open:MKT-001:0")
    assert {"tk:work:MKT-001:0", "tk:done:MKT-001:0", "tk:risk:MKT-001:0"} <= set(buttons(h, LOLA))

    # Start: the sheet says «В работе», the consultant hears about it once.
    await h.press(LOLA, "tk:work:MKT-001:0")
    assert sheet.cell("MKT-001", "status") == "В работе"
    assert any("Задача стартовала" in t and "MKT-001" in t for t in texts(h, DILNOZA))

    # Done: fact date, the approver gets Accept / Return, the informed get a note; Lola hears nothing.
    before = len(h.api.sent_to(LOLA))
    await h.press(LOLA, "tk:done:MKT-001:0")
    assert sheet.cell("MKT-001", "status") == "Готово" and sheet.cell("MKT-001", "fact") == serial(TODAY)
    to_bobur = h.api.sent_to(BOBUR)[-1]
    assert "Сдано на приёмку" in to_bobur.text and "Отметил(а): Лола" in to_bobur.text
    assert "tk:accept:MKT-001:0" in buttons(h, BOBUR)
    assert "Задача выполнена" in texts(h, TEMUR)[-1]
    assert all("Сдано на приёмку" not in t for t in texts(h, LOLA)[before:])

    # Bobur accepts: Lola is told; the task leaves his review list.
    await h.press(BOBUR, "tk:accept:MKT-001:0")
    assert "Задача принята" in texts(h, LOLA)[-1]
    async with factory() as s:
        st = await s.get(TaskState, "MKT-001")
    assert st.accepted_at is not None and st.accepted_by == "Бобур"
    await h.press(BOBUR, "tk:accept:MKT-001:0")
    assert "Задача уже принята или больше не в статусе «Готово»." in h.answers(BOBUR)

    await start(h, BOBUR)
    await h.send(BOBUR, "🗂 Задачи")
    assert "Ждут вашей приёмки: 1" in h.api.sent_to(BOBUR)[-1].text  # only MKT-003 now


async def test_risk_and_return_need_a_reason(h: Harness, sheet: FakeBackend, factory):
    await start(h, LOLA)
    await h.press(LOLA, "tk:risk:MKT-002:0")
    assert "Что мешает задаче MKT-002" in h.api.sent_to(LOLA)[-1].text
    await h.send(LOLA, "Ждём исходники от продакшна")
    assert sheet.cell("MKT-002", "status") == "Риск"
    assert sheet.cell("MKT-002", "comment") == f"{TODAY:%d.%m} Лола: Ждём исходники от продакшна"
    assert "Риск по задаче" in texts(h, BOBUR)[-1] and "Ждём исходники" in texts(h, BOBUR)[-1]

    # The approver sends finished work back: status «В работе», no fact date, the reason in the comment.
    await start(h, BOBUR)
    await h.press(BOBUR, "tk:return:MKT-003:0")
    await h.send(BOBUR, "Нет обложки для ММ")
    assert sheet.cell("MKT-003", "status") == "В работе" and sheet.cell("MKT-003", "fact") == ""
    assert "Нет обложки для ММ" in sheet.cell("MKT-003", "comment")
    to_dilnoza = texts(h, DILNOZA)[-1]
    assert "возвращена на доработку" in to_dilnoza and "Что доделать: Нет обложки для ММ" in to_dilnoza
    assert all("стартовала" not in t for t in texts(h, DILNOZA))  # a return is not a start


async def test_poll_turns_sheet_edits_into_events(h: Harness, sheet: FakeBackend, factory):
    await start(h, BOBUR)
    sheet.set("MKT-001", "status", "Готово")  # edited by hand in the spreadsheet
    async with factory() as s:
        changes = await board.poll(s)
    assert [(c.task.id, c.prev, c.new, c.source) for c in changes] == [("MKT-001", "Не начато", "Готово", "sheet")]
    from app.services.task_events import announce

    async with factory() as s:
        for c in changes:
            await announce(h.bot, s, Settings(bot_token="1:a", _env_file=None), board, c)
    assert "Сдано на приёмку" in texts(h, BOBUR)[-1] and "Отметил" not in texts(h, BOBUR)[-1]

    # The spreadsheet's test mode: everything goes to the test chat, marked with the addressee.
    board.test_chat_id = 999
    sheet.set("MKT-002", "status", "Отменено")
    sheet.set("MKT-002", "comment", "Медиаплан делает агентство")
    async with factory() as s:
        for c in await board.poll(s):
            await announce(h.bot, s, Settings(bot_token="1:a", _env_file=None), board, c)
    to_test = texts(h, 999)
    assert len(to_test) == 1 and "Тест · для: Маркетолог · Лола" in to_test[0] and "агентство" in to_test[0]


async def test_rights_and_clients(h: Harness, sheet: FakeBackend):
    await start(h, DILNOZA)  # consultant of MKT-001: may only flag a risk
    await h.press(DILNOZA, "tk:open:MKT-001:0")
    assert [b for b in buttons(h, DILNOZA) if b.startswith("tk:")] == ["tk:risk:MKT-001:0"]
    await h.press(DILNOZA, "tk:done:MKT-001:0")
    assert "Это действие вам недоступно для этой задачи." in h.answers(DILNOZA)
    assert sheet.cell("MKT-001", "status") == "Не начато"

    await start(h, CLIENT)  # not on «Команда»
    await h.press(CLIENT, "tk:open:MKT-001:0")
    assert any("Раздел задач — для сотрудников" in a for a in h.answers(CLIENT))
    await h.send(CLIENT, "/my")
    assert "Пока пусто" in h.api.sent_to(CLIENT)[-1].text  # a client's /my is "My tickets"


async def test_summary_for_heads(h: Harness, sheet: FakeBackend):
    await start(h, BOBUR)  # default approver of «Маркетинг» in «Справочник»
    await h.send(BOBUR, "🗂 Задачи")
    assert "tl:summary:0" in buttons(h, BOBUR)
    await h.press(BOBUR, "tl:summary:0")
    summary = edits(h)[-1]
    assert "<b>Маркетинг</b>: 2 (1) · ❗ 1 · ⚠️ 0 · 📥 1" in summary and "Маркетолог · Лола — " in summary
    await h.press(BOBUR, "tl:dept:0")
    dept = edits(h)[-1]
    assert "Просрочено</b> (1)" in dept and "MKT-002" in dept and "Ждут приёмки</b> (1)" in dept

    await start(h, LOLA)  # not a head
    await h.send(LOLA, "🗂 Задачи")
    assert "tl:summary:0" not in buttons(h, LOLA)
    await h.press(LOLA, "tl:summary:0")
    assert "Это действие вам недоступно для этой задачи." in h.answers(LOLA)


async def test_worker_polls_and_tells_the_script(h: Harness, sheet: FakeBackend, factory):
    from app import worker

    class FakeBridge:
        enabled = True

        def __init__(self):
            self.beats = []

        async def heartbeat(self, events):
            self.beats.append(events)
            return True, 777

    fake = FakeBridge()
    board.bridge, board.last_poll, board.last_heartbeat = fake, float("-inf"), float("-inf")
    sheet.set("MKT-002", "status", "Риск")
    settings = Settings(bot_token="1:a", _env_file=None)
    try:
        async with factory() as s:
            await worker.sync_tasks(h.bot, s, settings)
        assert fake.beats == [False, True] and board.test_chat_id == 777  # test mode learnt before anything is sent
        assert any("Риск по задаче" in t for t in texts(h, 777))  # announced, into the test chat

        # The sheet cannot be read: admins hear once, and the script is told to send events itself.
        async def broken():
            raise RuntimeError("403 The caller does not have permission")

        sheet.read = broken
        board.last_poll = board.last_heartbeat = float("-inf")
        async with factory() as s:
            await worker.sync_tasks(h.bot, s, settings)
        assert fake.beats == [False, True, True, False]  # still claimed on the regular beat, then given back
    finally:
        board.bridge, board.last_error, board.events_claimed = None, None, False
