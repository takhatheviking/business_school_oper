"""The task-status spreadsheet (the one the Apps Script in bot/ lives in): reading tasks,
the team and default approvers, and writing a task's status back.

Tasks live on department sheets: every sheet whose first row has «ID», «Задача»,
«Исполнитель (R)», «Статус» and «Срок», except the formula-built «Все задачи». Columns are
found by their headers, like the Apps Script does, so their order may change.

Access: the same Google service account as the Sheets mirror (GOOGLE_SERVICE_ACCOUNT_FILE),
shared on the status spreadsheet as an Editor. The spreadsheet id is STATUS_SHEET_ID.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol

import gspread
from gspread.utils import rowcol_to_a1

from app.services.team import Person, norm, split_list

ALL_TASKS_SHEET = "Все задачи"
TEAM_SHEET = "Команда"
REF_SHEET = "Справочник"

NEW, WORK, DONE, RISK, CANCELLED = "Не начато", "В работе", "Готово", "Риск", "Отменено"
STATUSES = (NEW, WORK, DONE, RISK, CANCELLED)
CLOSED = (DONE, CANCELLED)

COL = {
    "id": "ID", "task": "Задача", "r": "Исполнитель (R)", "status": "Статус", "start": "Начало", "due": "Срок",
    "fact": "Дата факт", "comment": "Комментарий", "week": "Неделя", "dept": "Отдел", "product": "Продукт",
    "result": "Результат (критерий готовности)", "hours": "Часы", "a": "Утверждает (A)", "c": "Консультирует (C)",
    "i": "Информировать (I)",
}
TASK_SHEET_COLS = ("id", "task", "r", "status", "due")
TEAM_COL = {
    "role": "Роль (как в задачах)", "name": "Имя", "dept": "Отдел", "aliases": "Другие названия в задачах",
    "username": "Telegram @username", "chat_id": "Telegram chat_id", "notify": "Уведомлять",
    "generic": "Общее название (по продукту)", "products": "Продукты",
}
READ_COLUMNS = 30  # department sheets carry a Gantt chart to the right of the table; it is not needed
SERIAL_EPOCH = date(1899, 12, 30)


@dataclass
class SheetValues:
    title: str
    gid: int
    values: list[list]  # UNFORMATTED_VALUE: numbers, strings, booleans; dates as serial numbers


@dataclass
class Task:
    id: str
    sheet: str
    gid: int
    row: int
    task: str
    r: str
    status: str
    due: date | None
    start: date | None = None
    fact: date | None = None
    comment: str = ""
    result: str = ""
    week: str = ""
    dept: str = ""
    product: str = ""
    hours: float | None = None
    a: str = ""
    c: str = ""
    i: str = ""

    @property
    def is_open(self) -> bool:
        return self.status not in CLOSED


def to_date(value) -> date | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return SERIAL_EPOCH + timedelta(days=int(value))
    if isinstance(value, str) and (m := re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", value)):
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    return None


def to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def header_index(head: list, spec: dict[str, str]) -> dict[str, int]:
    names = [to_text(h) for h in head]
    return {k: names.index(v) if v in names else -1 for k, v in spec.items()}


def is_task_sheet(title: str, head: list) -> bool:
    if title == ALL_TASKS_SHEET or not head:
        return False
    idx = header_index(head, COL)
    return all(idx[k] >= 0 for k in TASK_SHEET_COLS)


def parse_tasks(sheet: SheetValues) -> list[Task]:
    idx = header_index(sheet.values[0], COL)

    def cell(row: list, key: str):
        i = idx[key]
        return row[i] if 0 <= i < len(row) else None

    tasks = []
    for n, row in enumerate(sheet.values[1:], start=2):
        task_id = to_text(cell(row, "id"))
        if not task_id:
            continue
        hours = cell(row, "hours")
        tasks.append(
            Task(
                id=task_id, sheet=sheet.title, gid=sheet.gid, row=n,
                task=to_text(cell(row, "task")), r=to_text(cell(row, "r")), status=to_text(cell(row, "status")) or NEW,
                due=to_date(cell(row, "due")), start=to_date(cell(row, "start")), fact=to_date(cell(row, "fact")),
                comment=to_text(cell(row, "comment")), result=to_text(cell(row, "result")), week=to_text(cell(row, "week")),
                dept=to_text(cell(row, "dept")), product=to_text(cell(row, "product")),
                hours=float(hours) if isinstance(hours, (int, float)) and not isinstance(hours, bool) else None,
                a=to_text(cell(row, "a")), c=to_text(cell(row, "c")), i=to_text(cell(row, "i")),
            )
        )
    return tasks


def parse_team(values: list[list]) -> list[Person]:
    if not values:
        return []
    idx = header_index(values[0], TEAM_COL)
    people = []
    for n, row in enumerate(values[1:], start=2):
        g = lambda k: to_text(row[idx[k]]) if 0 <= idx[k] < len(row) else ""  # noqa: E731
        if not g("role"):
            continue
        people.append(
            Person(
                row=n, role=g("role"), name=g("name"), dept=g("dept"),
                aliases=split_list(g("aliases")), generic=split_list(g("generic")), products=split_list(g("products")),
                username=g("username").lstrip("@").lower(), chat_id=g("chat_id"), notify=norm(g("notify")) != "нет",
            )
        )
    return people


def parse_default_approvers(values: list[list], depts: set[str]) -> dict[str, str]:
    """«Отдел → Утверждает по умолчанию (A)» from «Справочник» (columns B and C, as in the Apps Script)."""
    result: dict[str, str] = {}
    for row in values:
        d = to_text(row[1]) if len(row) > 1 else ""
        a = to_text(row[2]) if len(row) > 2 else ""
        if d in depts and a and d not in result:
            result[d] = a
    return result


@dataclass
class Book:
    """Everything read from the spreadsheet in one go."""

    tasks: list[Task]
    people: list[Person]
    default_a: dict[str, str]
    url: str
    by_id: dict[str, Task] = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {}
        for t in self.tasks:
            self.by_id.setdefault(t.id, t)  # a duplicated ID: the first row wins

    def link(self, task: Task) -> str:
        return f"{self.url}#gid={task.gid}&range=A{task.row}"


class Backend(Protocol):
    url: str

    async def read(self) -> tuple[list[SheetValues], list[list], list[list]]:
        """Department sheets (all sheets, really), «Команда», «Справочник»."""

    async def write(self, task: Task, values: dict[str, object]) -> Task | None:
        """Write column values (by COL key) into the row with this task's ID; returns the
        task as it is now in the sheet, or None if the ID is gone."""


def build_book(sheets: list[SheetValues], team: list[list], ref: list[list], url: str) -> Book:
    tasks = [t for s in sheets if s.values and is_task_sheet(s.title, s.values[0]) for t in parse_tasks(s)]
    return Book(tasks, parse_team(team), parse_default_approvers(ref, {t.dept for t in tasks}), url)


class GspreadBackend:
    """Two API calls per read (sheet list once, then one batch of values), three per write."""

    def __init__(self, spreadsheet_id: str, key_file: str) -> None:
        self.spreadsheet_id, self.key_file = spreadsheet_id, key_file
        self.url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        self._sh: gspread.Spreadsheet | None = None

    def _book(self) -> gspread.Spreadsheet:
        if self._sh is None:
            client = gspread.service_account(filename=self.key_file, scopes=["https://www.googleapis.com/auth/spreadsheets"])
            self._sh = client.open_by_key(self.spreadsheet_id)
        return self._sh

    def _read(self) -> tuple[list[SheetValues], list[list], list[list]]:
        sh = self._book()
        worksheets = sh.worksheets()
        last = rowcol_to_a1(1, READ_COLUMNS).rstrip("1")
        ranges = [f"'{ws.title}'!A1:{last}{ws.row_count}" for ws in worksheets]
        data = sh.values_batch_get(ranges, params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"})
        sheets = [SheetValues(ws.title, ws.id, vr.get("values", [])) for ws, vr in zip(worksheets, data.get("valueRanges", []))]
        by_title = {s.title: s.values for s in sheets}
        return sheets, by_title.get(TEAM_SHEET, []), by_title.get(REF_SHEET, [])

    def _write(self, task: Task, values: dict[str, object]) -> Task | None:
        ws = self._book().get_worksheet_by_id(task.gid)
        last = rowcol_to_a1(1, READ_COLUMNS).rstrip("1")
        head = ws.row_values(1)
        idx = header_index(head, COL)
        ids = ws.col_values(idx["id"] + 1)
        try:
            row = next(n for n, v in enumerate(ids, start=1) if n > 1 and str(v).strip() == task.id)
        except StopIteration:
            return None
        cells = []
        for key, value in values.items():
            col = idx.get(key, -1)
            if col < 0:
                continue
            if key == "comment" and value:
                value = "'" + str(value)  # always text: a comment starting with "=" must not become a formula
            cells.append({"range": rowcol_to_a1(row, col + 1), "values": [[value]]})
        ws.batch_update(cells, value_input_option="USER_ENTERED")
        fresh = ws.get(f"A{row}:{last}{row}", value_render_option="UNFORMATTED_VALUE", date_time_render_option="SERIAL_NUMBER")
        updated = parse_tasks(SheetValues(ws.title, ws.id, [head, list(fresh[0]) if fresh else []]))
        if not updated:
            return None
        updated[0].row = row
        return updated[0]

    async def read(self) -> tuple[list[SheetValues], list[list], list[list]]:
        return await asyncio.to_thread(self._read)

    async def write(self, task: Task, values: dict[str, object]) -> Task | None:
        return await asyncio.to_thread(self._write, task, values)
