"""Общая консоль rich и мелкие помощники вывода (палитра совпадает с прежней версией)."""

from __future__ import annotations

import shutil

import pyfiglet
from rich.console import Console, Group, RenderableType
from rich.markup import escape
from rich.measure import Measurement
from rich.rule import Rule
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from futures_arb_bot.core.engine.formatting import GREY

console = Console(highlight=False, emoji=False, soft_wrap=False)
"""Консоль stdout: без автоподсветки чисел и замены :эмодзи: — как Spectre.Console."""

err_console = Console(stderr=True, highlight=False, emoji=False)

CYAN1 = "cyan1"


def esc(value: object) -> str:
    """Экранирование данных для rich-разметки (аналог Markup.Escape)."""
    return escape(str(value))


def figlet(text: str, color: str = CYAN1) -> Text:
    """Крупный заголовок шрифтом «standard» (тот же, что у FigletText в Spectre.Console)."""
    width = max(20, console.width if console.is_terminal else shutil.get_terminal_size().columns)
    art = pyfiglet.figlet_format(text, font="standard", width=width).rstrip("\n")
    return Text(art, style=color, no_wrap=True, overflow="crop")


class LeftRule:
    """Горизонтальная линия с заголовком слева: «── заголовок ─────» (как Rule.LeftJustified в Spectre)."""

    def __init__(self, title: str, style: str = GREY) -> None:
        self.title = title
        self.style = style

    def __rich_console__(self, console: Console, options):  # type: ignore[no-untyped-def]
        width = options.max_width
        title = Text.from_markup(self.title)
        title.truncate(max(0, width - 6), overflow="ellipsis")
        line_style = console.get_style(self.style)
        prefix = "── "
        rest = max(0, width - len(prefix) - title.cell_len - 1)
        yield Segment(prefix, line_style)
        yield from title.render(console)
        yield Segment(" " + "─" * rest, line_style)
        yield Segment.line()

    def __rich_measure__(self, console: Console, options) -> Measurement:  # type: ignore[no-untyped-def]
        return Measurement(options.max_width, options.max_width)


def center_rule(title: str, style: str) -> Rule:
    return Rule(title, style=style, align="center", characters="─")


def plain_rule(style: str = GREY) -> Rule:
    return Rule(style=Style.parse(style), characters="─")


def stack(*items: RenderableType) -> Group:
    return Group(*items)
