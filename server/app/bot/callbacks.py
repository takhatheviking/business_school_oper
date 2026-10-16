from aiogram.filters.callback_data import CallbackData


class LangCb(CallbackData, prefix="lg"):
    lang: str


class EventsCb(CallbackData, prefix="ev"):
    action: str  # list | open | buy
    event_id: int = 0
    page: int = 0


class TypeCb(CallbackData, prefix="tt"):
    ticket_type_id: int


class QtyCb(CallbackData, prefix="qt"):
    ticket_type_id: int
    qty: int


class BookCb(CallbackData, prefix="bk"):
    action: str  # confirm | abort
    ticket_type_id: int = 0
    qty: int = 0


class OrderCb(CallbackData, prefix="or"):
    action: str  # show | cancel | proof
    order_id: int


class MyCb(CallbackData, prefix="my"):
    action: str  # ticket | list
    item_id: int = 0


class ModCb(CallbackData, prefix="md"):
    action: str  # ok | no | reason | back | refunded
    payment_id: int
    reason: str = ""


class CheckInCb(CallbackData, prefix="ci"):
    token: str  # the QR secret, so staff cannot check in arbitrary tickets by id


class CourseCb(CallbackData, prefix="cs"):
    action: str  # list | open
    course_id: int = 0


class LeadCb(CallbackData, prefix="ld"):
    action: str  # start | send | abort
    course_id: int = 0  # 0: a general consultation


class SalesCb(CallbackData, prefix="sl"):
    action: str  # take | enrolled | rejected | reopen
    lead_id: int


class TaskCb(CallbackData, prefix="tk"):
    action: str  # open | work | done | risk | accept | return | cancel
    task_id: str
    page: int = 0  # the list page to come back to


class TasksCb(CallbackData, prefix="tl"):
    action: str  # home | mine | review | summary | dept
    page: int = 0  # list page, or the department's number on the summary


class AdminCb(CallbackData, prefix="ad"):
    action: str
    event_id: int = 0
    item_id: int = 0
    value: str = ""
