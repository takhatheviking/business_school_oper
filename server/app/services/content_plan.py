"""The launch content plan: a Google Sheet with one row per channel post (docs/CHANNEL.md).

The marketing team keeps it on a sheet like «Kontent reja»: a few title rows, then a header
row with «№», «Sana», «Vaqt», «Format», «Sarlavha / Hook», «TELEGRAMGA TAYYOR POST MATNI»,
«Mas’ul» and «Status». Columns are found by their headers (Uzbek, Russian or English), so
they may move; the plan is the first sheet that has such a header row.

The bot only reads the plan and writes one column, «Status»: «Tayyor» when a file for the
post arrives, «Joylandi» once the post is out. The sheet's own values are Rejada / Tayyor /
Joylandi / Bekor qilindi.

Access: the Google service account of GOOGLE_SERVICE_ACCOUNT_FILE, shared on the sheet as an
Editor (a Viewer is enough to post, but then statuses are not written back).
"""

import asyncio
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import gspread
from gspread.utils import rowcol_to_a1

SERIAL_EPOCH = date(1899, 12, 30)
HEADER_ROWS = 10  # the header row is looked for among the first rows of a sheet
READ_COLUMNS = 30

# Format kinds: what the bot does with a post.
TEXT, PHOTO, DOCUMENT, MANUAL = "text", "photo", "document", "manual"
AUTO_KINDS = (TEXT, PHOTO, DOCUMENT)  # the bot publishes these; MANUAL ones (round videos) people post

# Statuses as the sheet's dropdown has them.
PLANNED, READY, POSTED, CANCELLED = "Rejada", "Tayyor", "Joylandi", "Bekor qilindi"
POSTED_WORDS = {"joylandi", "joylangan", "опубликован", "опубликовано", "posted", "published"}
CANCELLED_WORDS = {"bekor qilindi", "bekor", "отменён", "отменен", "отменено", "cancelled", "canceled"}

HEADERS: dict[str, tuple[str, ...]] = {
    "no": ("№", "#", "no", "id"),
    "date": ("sana", "дата", "date"),
    "time": ("vaqt", "время", "time"),
    "stage": ("bosqich", "этап", "stage"),
    "format": ("format", "формат"),
    "title": ("sarlavha", "заголовок", "title", "hook"),
    "text": ("telegramga tayyor post matni", "post matni", "текст поста", "post text"),
    "owner": ("mas'ul", "ответственный", "owner"),
    "status": ("status", "статус"),
}
REQUIRED = ("no", "date", "time", "format", "text")
APOSTROPHES = str.maketrans({c: "'" for c in "‘’ʻʼ`´"})


def norm(value: object) -> str:
    return " ".join(str(value or "").translate(APOSTROPHES).lower().split())


def to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def header_match(cell: object, aliases: tuple[str, ...]) -> bool:
    h = norm(cell)
    return any(h == a or h.startswith(a + " ") or h.startswith(a + "/") for a in aliases)


def find_header(values: list[list]) -> tuple[int, dict[str, int]] | None:
    """(0-based row of the header, column of each key: -1 if absent), or None if no row has
    every required header."""
    for r, row in enumerate(values[:HEADER_ROWS]):
        idx = {}
        for key, aliases in HEADERS.items():
            idx[key] = next((c for c, cell in enumerate(row) if header_match(cell, aliases)), -1)
        if all(idx[k] >= 0 for k in REQUIRED):
            return r, idx
    return None


def format_kind(value: str) -> str:
    f = norm(value)
    if not f or f in ("yozuv", "matn", "text", "текст"):
        return TEXT
    if "pdf" in f:
        return DOCUMENT
    if any(w in f for w in ("video", "видео", "dumaloq", "кружок", "aylana")):
        return MANUAL
    if any(w in f for w in ("banner", "баннер", "rasm", "фото", "photo", "image", "картинка", "dizayn")):
        return PHOTO
    return MANUAL  # something the bot cannot publish by itself


def to_date(value: object) -> date | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return SERIAL_EPOCH + timedelta(days=int(value))
    text = to_text(value)
    if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", text):
        day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    elif m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def to_time(value: object) -> time | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fraction = float(value) % 1  # a time cell is a fraction of a day
        minutes = round(fraction * 1440) % 1440
        return time(minutes // 60, minutes % 60)
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::\d{2})?", to_text(value))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    return time(int(m.group(1)), int(m.group(2)))


@dataclass
class PlanPost:
    no: str
    row: int  # 1-based row in the sheet
    due: datetime | None  # aware, in the school's timezone; None if the date or time is unreadable
    kind: str
    format: str  # as written in the sheet
    title: str
    text: str
    owner: str = ""
    status: str = ""
    stage: str = ""

    @property
    def posted(self) -> bool:
        return norm(self.status) in POSTED_WORDS

    @property
    def cancelled(self) -> bool:
        return norm(self.status) in CANCELLED_WORDS

    @property
    def needs_file(self) -> bool:
        return self.kind in (PHOTO, DOCUMENT)

    @property
    def label(self) -> str:
        """A short name for buttons and messages: the hook, or the start of the text."""
        source = self.title or self.text
        first = source.strip().splitlines()[0] if source.strip() else ""
        return first[:80]


def parse_posts(values: list[list], tz: ZoneInfo) -> list[PlanPost]:
    found = find_header(values)
    if found is None:
        return []
    head_row, idx = found

    def cell(row: list, key: str) -> object:
        i = idx[key]
        return row[i] if 0 <= i < len(row) else None

    posts, seen = [], set()
    for n, row in enumerate(values[head_row + 1:], start=head_row + 2):
        no = to_text(cell(row, "no"))
        if not no or no in seen:
            continue  # an empty row, or a duplicated number: the first row wins
        seen.add(no)
        d, t = to_date(cell(row, "date")), to_time(cell(row, "time"))
        if t is None and isinstance(cell(row, "date"), float):
            t = to_time(cell(row, "date"))  # date and time in one cell
        fmt = to_text(cell(row, "format"))
        posts.append(
            PlanPost(
                no=no, row=n, due=datetime.combine(d, t, tzinfo=tz) if d and t else None, kind=format_kind(fmt), format=fmt,
                title=to_text(cell(row, "title")), text=str(cell(row, "text") or "").strip(), owner=to_text(cell(row, "owner")),
                status=to_text(cell(row, "status")), stage=to_text(cell(row, "stage")),
            )
        )
    return posts


@dataclass
class Plan:
    sheet_id: str
    tab: str
    gid: int
    url: str
    posts: list[PlanPost]
    by_no: dict[str, PlanPost] = field(init=False)

    def __post_init__(self) -> None:
        self.posts.sort(key=lambda p: (p.due is None, p.due.timestamp() if p.due else 0.0, p.row))
        self.by_no = {p.no: p for p in self.posts}

    def link(self, post: PlanPost) -> str:
        return f"{self.url}#gid={self.gid}&range=A{post.row}"


class PlanBackend(Protocol):
    sheet_id: str
    url: str

    async def read(self) -> tuple[str, int, list[list]]:
        """(title, gid, values) of the plan sheet; raises LookupError if no sheet looks like a plan."""

    async def write_status(self, gid: int, no: str, value: str) -> bool:
        """Write the status of post `no` (found by number, rows may have moved); False if it is gone."""


class GspreadPlanBackend:
    """One call to list sheets (once), one batch read per poll, three calls per status write."""

    def __init__(self, sheet_id: str, key_file: str | None) -> None:
        self.sheet_id, self.key_file = sheet_id, key_file
        self.url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        self._sh: gspread.Spreadsheet | None = None

    def _book(self) -> gspread.Spreadsheet:
        if self._sh is None:
            if not self.key_file or not os.path.exists(self.key_file):
                raise FileNotFoundError(f"no Google service account key at {self.key_file} (docs/GOOGLE_SHEETS.md)")
            client = gspread.service_account(filename=self.key_file, scopes=["https://www.googleapis.com/auth/spreadsheets"])
            self._sh = client.open_by_key(self.sheet_id)
        return self._sh

    def _read(self) -> tuple[str, int, list[list]]:
        sh = self._book()
        worksheets = sh.worksheets()
        last = rowcol_to_a1(1, READ_COLUMNS).rstrip("1")
        ranges = [f"'{ws.title}'!A1:{last}{ws.row_count}" for ws in worksheets]
        data = sh.values_batch_get(ranges, params={"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "SERIAL_NUMBER"})
        for ws, vr in zip(worksheets, data.get("valueRanges", [])):
            values = vr.get("values", [])
            if find_header(values) is not None:
                return ws.title, ws.id, values
        raise LookupError("no sheet with a content-plan header row")

    def _write_status(self, gid: int, no: str, value: str) -> bool:
        ws = self._book().get_worksheet_by_id(gid)
        last = rowcol_to_a1(1, READ_COLUMNS).rstrip("1")
        values = ws.get(f"A1:{last}{ws.row_count}", value_render_option="UNFORMATTED_VALUE")
        found = find_header(values)
        if found is None or found[1]["status"] < 0:
            return False
        head_row, idx = found
        for n, row in enumerate(values[head_row + 1:], start=head_row + 2):
            if idx["no"] < len(row) and to_text(row[idx["no"]]) == no:
                ws.update_cell(n, idx["status"] + 1, value)
                return True
        return False

    async def read(self) -> tuple[str, int, list[list]]:
        return await asyncio.to_thread(self._read)

    async def write_status(self, gid: int, no: str, value: str) -> bool:
        return await asyncio.to_thread(self._write_status, gid, no, value)
