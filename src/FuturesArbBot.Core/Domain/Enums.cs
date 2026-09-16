namespace FuturesArbBot.Core.Domain;

/// <summary>Режим работы робота.</summary>
public enum NetworkMode
{
    /// <summary>Симуляция: сделки виртуальные, ордера на биржу не отправляются.</summary>
    DryRun,

    /// <summary>Тестовая сеть биржи (testnet): реальные ордера, но условные деньги.</summary>
    Testnet,

    /// <summary>Реальная торговля настоящими деньгами. Требует AllowLive = true.</summary>
    Live,
}

/// <summary>Направление ордера.</summary>
public enum OrderSide
{
    Buy,
    Sell,
}

/// <summary>Тип ордера.</summary>
public enum OrderType
{
    /// <summary>Рыночный: исполняется сразу по доступной ликвидности.</summary>
    Market,

    /// <summary>Лимитный: исполняется по указанной цене или лучше.</summary>
    Limit,

    /// <summary>
    /// Лимитный с догонанием цены (chase): заявка переставляется ближе к рынку,
    /// пока не исполнится или не будет исчерпан бюджет шагов. Что именно уйдёт
    /// на биржу — локальные перестановки (Simulated) или нативный chase биржи —
    /// решает <see cref="OrderPolicyResolver"/> исходя из настроек и возможностей биржи.
    /// </summary>
    ChaseLimit,
}

/// <summary>Условия действия заявки (time in force).</summary>
public enum TimeInForce
{
    /// <summary>Живёт в стакане до полного исполнения или отмены.</summary>
    Gtc,

    /// <summary>Неисполненный остаток отменяется сразу после попытки исполнения.</summary>
    Ioc,

    /// <summary>Полное исполнение или отмена.</summary>
    Fok,

    /// <summary>Только мейкер: заявка не должна разбрать ликвидность (post-only).</summary>
    PostOnly,
}

/// <summary>Способ догонания цены лимитной заявкой.</summary>
public enum ChaseMode
{
    /// <summary>Догонания нет: обычная лимитная заявка.</summary>
    None,

    /// <summary>
    /// Локальное (симулируемое) догонание: исполнитель сам снимает заявку и
    /// переставляет её по шагу. Работает на любой бирже с cancel + fetch order.
    /// </summary>
    Simulated,

    /// <summary>
    /// Нативный «chase order» биржи (есть не везде, например KuCoin Futures):
    /// догонанием управляет биржа, робот только выставляет одну заявку.
    /// </summary>
    Native,
}

/// <summary>Статус арбитражной позиции.</summary>
public enum PositionStatus
{
    Open,
    Closed,
}

/// <summary>Причина закрытия позиции.</summary>
public enum CloseReason
{
    /// <summary>Спред сжался до порога «вниз» — фиксация прибыли.</summary>
    TakeProfit,

    /// <summary>Спред вырос до стоп-лосса.</summary>
    StopLoss,

    /// <summary>Позиция достигла максимального возраста.</summary>
    Timeout,

    /// <summary>Закрытие при остановке робота (Ctrl+C).</summary>
    SessionEnd,

    /// <summary>Аварийный откат: вторая нога не открылась, первая закрыта.</summary>
    Rollback,
}

/// <summary>Уровень события журнала.</summary>
public enum AppLogLevel
{
    Debug,
    Info,
    Success,
    Warning,
    Error,
}
