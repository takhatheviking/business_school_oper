"""Google Sheets mirror against an in-memory fake of the gspread API."""

import json

from app.config import Settings
from app.services.booking import approve_payment, create_order, submit_proof
from app.services.gsheets import SheetsSync, parse_sheet_id
from app.services.tickets import check_in
from tests.conftest import make_event, make_user

SHEET_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcd"


class FakeWorksheet:
    def __init__(self, ws_id: int, title: str, rows: int, cols: int):
        self.id, self.title, self.row_count, self.col_count = ws_id, title, rows, cols
        self.values: list[list] | None = None
        self.writes = 0

    def update_title(self, title):
        self.title = title

    def resize(self, rows=None, cols=None):
        self.row_count, self.col_count = rows or self.row_count, cols or self.col_count

    def batch_clear(self, ranges):
        self.values = None

    def update(self, values, range_name, raw=True):
        assert raw, "values must be written RAW (no formulas from user text)"
        self.values, self.writes = values, self.writes + 1


class FakeSpreadsheet:
    title = "Proactive reports"

    def __init__(self):
        self.tabs: dict[int, FakeWorksheet] = {}
        self.requests: list[dict] = []

    def worksheets(self):
        return list(self.tabs.values())

    def add_worksheet(self, title, rows, cols):
        ws = FakeWorksheet(100 + len(self.tabs), title, rows, cols)
        self.tabs[ws.id] = ws
        return ws

    def batch_update(self, body):
        self.requests.extend(body["requests"])


class FakeClient:
    def __init__(self):
        self.spreadsheet = FakeSpreadsheet()

    def open_by_key(self, key):
        assert key == SHEET_ID
        return self.spreadsheet


def make_sync(tmp_path) -> tuple[SheetsSync, FakeClient, Settings]:
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"client_email": "bot@project.iam.gserviceaccount.com"}))
    settings = Settings(bot_token="1:x", google_service_account_file=str(key), google_sheet_id=SHEET_ID, _env_file=None)
    sync, client = SheetsSync(), FakeClient()
    sync._client_for = lambda _settings: client
    return sync, client, settings


async def test_sync_creates_tabs_and_skips_unchanged(session, tmp_path):
    sync, client, settings = make_sync(tmp_path)
    _, tt = await make_event(session)
    user = await make_user(session, 1, username="buyer")
    order, _ = await create_order(session, user=user, ticket_type_id=tt.id, quantity=1, hold_minutes=30, max_per_order=5)
    await session.commit()
    _, payment, _ = await submit_proof(session, order_id=order.id, user_id=user.id, file_id="f", file_unique_id="u", kind="photo")
    await session.commit()
    result = await approve_payment(session, payment_id=payment.id, moderator_tg_id=9, moderator_name="Mod")
    await session.commit()

    assert await sync.sync(session, settings) == 3  # "Leads" + "All orders" + the event tab
    tabs = {ws.title: ws for ws in client.spreadsheet.worksheets()}
    assert "Все заказы" in tabs and "Заявки" in tabs
    event_tab = next(ws for title, ws in tabs.items() if title.startswith("#"))
    header = event_tab.values[4]
    row = event_tab.values[5]
    assert header[1] == "Код билета"
    assert row[1] == result.tickets[0].code and row[3] == "+998900000000" and row[4] == "@buyer"
    assert any("addBanding" in r for r in client.spreadsheet.requests)

    # Nothing changed: no writes.
    assert await sync.sync(session, settings) == 0

    # A check-in changes only the participants tab.
    staff = await make_user(session, 2, role="staff")
    await check_in(session, ticket_id=result.tickets[0].id, staff=staff)
    await session.commit()
    assert await sync.sync(session, settings) == 1
    assert event_tab.values[5][6] == "Прошёл" and event_tab.writes == 2


async def test_tab_mapping_survives_restart(session, tmp_path):
    sync, client, settings = make_sync(tmp_path)
    await make_event(session)
    await sync.sync(session, settings)
    created = len(client.spreadsheet.tabs)

    # A fresh process: in-memory hashes are gone, the tab ids come from the DB.
    restarted = SheetsSync()
    restarted._client_for = lambda _settings: client
    await restarted.sync(session, settings)
    assert len(client.spreadsheet.tabs) == created  # rewritten in place, no duplicates


async def test_leads_tab_follows_status_changes(session, tmp_path):
    from app.services.courses import change_lead_status, create_lead

    sync, client, settings = make_sync(tmp_path)
    user = await make_user(session, 7, username="client", source="ig_reels")
    lead, created = await create_lead(session, user=user, course_id=None, name="Азиз", phone="+998901234567", comment="=HYPERLINK(1)", lang="uz")
    assert created
    await sync.sync(session, settings)
    leads_tab = {ws.title: ws for ws in client.spreadsheet.worksheets()}["Заявки"]
    row = leads_tab.values[5]
    assert row[0] == lead.id and row[2] == "новая" and row[3] == "Азиз" and row[6] == "Консультация"
    assert row[7] == "=HYPERLINK(1)" and row[8] == "UZ" and row[9] == "ig_reels"  # written RAW: no formula injection

    await change_lead_status(session, lead_id=lead.id, action="take", manager_tg_id=5, manager_name="Продавец")
    assert await sync.sync(session, settings) == 1
    assert leads_tab.values[5][2] == "в работе" and leads_tab.values[5][10] == "Продавец"


def test_parse_sheet_id():
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0"
    assert parse_sheet_id(url) == SHEET_ID
    assert parse_sheet_id(SHEET_ID) == SHEET_ID
    assert parse_sheet_id("https://example.com/x") is None
