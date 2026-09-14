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
