"""Текст исключения для журнала: как Exception.Message — никогда не пустой."""

from __future__ import annotations

from futures_arb_bot.core.cancellation import OperationCanceledError


def message_of(ex: BaseException) -> str:
    if isinstance(ex, OperationCanceledError) and not str(ex):
        return "The operation was canceled."
    text = str(ex)
    if text:
        return text
    return type(ex).__name__
