"""People of the task-status spreadsheet and how task cells are matched to them.

A Python port of the Apps Script in bot/Code.gs (norm_, aliasRegex_, buildMatcher_,
matchPeople_, personKey_): the spreadsheet's script and this server must agree on who
the executor (R), approver (A), consulted (C) and informed (I) of a task are.
Keep both in sync; tests/test_team.py repeats the cases of bot/test/run.js.
"""

import re
from dataclasses import dataclass, field

# Words in the "Исполнитель" cell that are not roles (CONFIG.IGNORE_WORDS in Code.gs).
IGNORE_WORDS = ("проверка", "на встречах")
GROUP_USERNAME = "группа"  # a role answered by a group chat, not a person


def norm(value: object) -> str:
    return re.sub(r"\s+", " ", str("" if value is None else value).lower().replace("ё", "е")).strip()


def split_list(value: object) -> list[str]:
    return [x.strip() for x in re.split(r"[;\n]", str(value or "")) if x.strip()]


def alias_re(alias: str) -> re.Pattern:
    # JS: (^|[^\p{L}\p{N}]) alias (?=$|[^\p{L}\p{N}]). In Python \w is letters, digits and "_",
    # so "not a letter or digit" is [\W_].
    return re.compile(r"(^|[\W_])" + re.escape(alias) + r"(?=$|[\W_])")


@dataclass(eq=False)
class Person:
    """A row of the "Команда" sheet. One human may have several rows (roles)."""

    row: int
    role: str
    name: str = ""
    dept: str = ""
    aliases: list[str] = field(default_factory=list)
    generic: list[str] = field(default_factory=list)
    products: list[str] = field(default_factory=list)
    username: str = ""  # lower case, without "@"; "группа" for a group chat
    chat_id: str = ""
    notify: bool = True

    @property
    def key(self) -> str:
        """Same as personKey_: one key per Telegram chat, so a person with two roles gets one message."""
        if self.chat_id:
            return f"c{self.chat_id}"
        if self.username and self.username != GROUP_USERNAME:
            return f"u{self.username}"
        return f"r{self.row}"

    @property
    def label(self) -> str:
        return f"{self.role} · {self.name}" if self.name and norm(self.name) != norm(self.role) else self.role


@dataclass
class _Alias:
    alias: str
    generic: bool
    people: list[Person]
    pattern: re.Pattern


def build_matcher(people: list[Person]) -> list[_Alias]:
    """Every name of every role, longest first: «Монтажёр №2» is tried before «Монтажёр»."""
    entries: dict[str, _Alias] = {}

    def add(alias: str, person: Person, generic: bool) -> None:
        alias = norm(alias)
        if not alias:
            return
        key = ("g:" if generic else "s:") + alias
        entry = entries.setdefault(key, _Alias(alias, generic, [], alias_re(alias)))
        if person not in entry.people:
            entry.people.append(person)

    for p in people:
        add(p.role, p, False)
        add(p.name, p, False)
        for a in p.aliases:
            add(a, p, False)
        for a in p.generic:
            add(a, p, True)
    return sorted(entries.values(), key=lambda e: (-len(e.alias), e.generic))


def match_people(text: str, product: str, matcher: list[_Alias]) -> tuple[list[Person], str]:
    """People named in a cell, and the rest that matched nobody. A generic name
    («методист») narrows down to the roles of the task's product when there are such."""
    t = f" {norm(text)} "
    found: list[Person] = []
    for i, e in enumerate(matcher):
        if not e.pattern.search(t):
            continue
        cands = e.people
        if e.generic:
            prod = norm(product)
            narrowed = [p for p in cands if any(norm(x) == prod for x in p.products)]
            cands = narrowed or cands
        for p in cands:
            if p not in found:
                found.append(p)
        # The same name may belong to several entries: remove it only after the last one.
        nxt = matcher[i + 1] if i + 1 < len(matcher) else None
        if nxt is None or nxt.alias != e.alias:
            t = e.pattern.sub(r"\1 ", t)
    rest = re.sub(r"[+/,;:]", " ", re.sub(r"\([^)]*\)", " ", t))
    for w in (*IGNORE_WORDS, "и"):
        rest = alias_re(norm(w)).sub(r"\1 ", rest)
    return found, re.sub(r"\s+", " ", rest).strip()
