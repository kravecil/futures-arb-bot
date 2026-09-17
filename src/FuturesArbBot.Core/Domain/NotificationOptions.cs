using System.ComponentModel.DataAnnotations;

namespace FuturesArbBot.Core.Domain;

/// <summary>Способ отправки сообщения через MAX Bot API.</summary>
public enum MaxSendEndpoint
{
    /// <summary>Актуальный v2-путь: POST /messages?chat_id=… (рекомендуется).</summary>
    Messages,

    /// <summary>Запасной путь: POST /chats/{chat_id}/message (объявлен устаревшим, если v2 откажет).</summary>
    ChatsMessage,
}

/// <summary>
/// Настройки уведомлений администратору (config/notifications.json → Notifications).
/// Раздел необязателен: отсутствие файла означает «выключено», существующие инсталляции не ломаются.
/// Секрет (BotToken) лучше задавать переменной окружения ARB_Notifications__BotToken, а не файлом.
/// </summary>
public sealed class NotificationOptions
{
    /// <summary>Базовый адрес Bot API v2 (используется и для проверки токена, и для отправки).</summary>
    public const string DefaultBaseUrl = "https://platform-api2.max.ru";

    /// <summary>Выключает раздел целиком — очередь не создаётся, запросы не отправляются.</summary>
    public bool Enabled { get; set; }

    /// <summary>Токен бота, выданный @MasterBot. В заголовке передаётся без префикса Bearer.</summary>
    public string BotToken { get; set; } = string.Empty;

    /// <summary>chat_id диалога администратора. Бот не может написать первым — диалог должен начать человек.</summary>
    public string AdminChatId { get; set; } = string.Empty;

    /// <summary>База Bot API. Для новых интеграций — https://platform-api2.max.ru.</summary>
    [Url]
    public string ApiBaseUrl { get; set; } = DefaultBaseUrl;

    /// <summary>Какой путь использовать для отправки (см. <see cref="MaxSendEndpoint"/>).</summary>
    [EnumDataType(typeof(MaxSendEndpoint))]
    public MaxSendEndpoint Endpoint { get; set; } = MaxSendEndpoint.Messages;

    /// <summary>Дополнительный порог: слать только спреды не уже этого значения в % (null — порог входа арбитража).</summary>
    [Range(0, 100)]
    public decimal? MinSpreadPercent { get; set; }

    /// <summary>Форматировать текст как markdown (поле format: «markdown»). По умолчанию — обычный текст.</summary>
    public bool UseMarkdown { get; set; }

    /// <summary>True — «молчаливая» доставка (notify: false): сообщение придёт без push-звука.</summary>
    public bool Silent { get; set; }

    /// <summary>Пауза между уведомлениями по одному символу, секунд.</summary>
    [Range(0, 86_400)]
    public int SignalCooldownSeconds { get; set; } = 120;

    /// <summary>Потолок сообщений в минуту сверх кулдауна (защита от флуда при многих символах).</summary>
    [Range(1, 60)]
    public int MaxPerMinute { get; set; } = 20;

    /// <summary>Таймаут одного HTTP-запроса, мс.</summary>
    [Range(500, 60_000)]
    public int TimeoutMs { get; set; } = 5_000;

    /// <summary>Сколько раз повторить отправку одного сообщения (429/5xx/сеть).</summary>
    [Range(0, 10)]
    public int MaxRetries { get; set; } = 3;

    /// <summary>После стольких неудачных отправок подряд уведомления приостанавливаются.</summary>
    [Range(1, 100)]
    public int FailureCircuitLimit { get; set; } = 5;

    /// <summary>Длительность паузы после срабатывания защиты, минут.</summary>
    [Range(1, 720)]
    public int CircuitBreakMinutes { get; set; } = 10;

    /// <summary>Вместимость очереди отправителя; переполнение — самые новые сигналы отбрасываются.</summary>
    [Range(8, 1024)]
    public int QueueCapacity { get; set; } = 64;

    /// <summary>
    /// Шаблон текста. Доступные подстановки: {symbol} {longExchange} {longPrice} {shortExchange}
    /// {shortPrice} {net} {gross} {longFee} {shortFee} {longFunding} {shortFunding} {volume} {time}.
    /// </summary>
    public string TextTemplate { get; set; } = DefaultTextTemplate;

    /// <summary>Шаблон сообщения по умолчанию (одна строка заголовка + две строки деталей).</summary>
    public const string DefaultTextTemplate =
        "Арбитраж {symbol}: нетто {net} (gross {gross})\n"
        + "лонг {longExchange} @ {longPrice} → шорт {shortExchange} @ {shortPrice}\n"
        + "фандинг {longFunding}/{shortFunding} · объём {volume} $ · {time}";

    /// <summary>Раздел реально может отправлять сообщения.</summary>
    public bool IsUsable => Enabled
        && !string.IsNullOrWhiteSpace(BotToken)
        && !string.IsNullOrWhiteSpace(AdminChatId);
}

/// <summary>Исход попытки доставки уведомления во внешний канал.</summary>
public enum NotificationOutcome
{
    /// <summary>Сообщение принято мессенджером.</summary>
    Delivered,

    /// <summary>Временный сбой (сеть, 5xx, лимит 429): повтор возмётся позже.</summary>
    Transient,

    /// <summary>Постоянная ошибка (неверный токен, нет диалога, запрещённый формат): повтор бессмысленен.</summary>
    Permanent,
}

/// <summary>Результат доставки: исход плюс человекочитаемое описание ошибки для журнала.</summary>
public sealed record NotificationDelivery(NotificationOutcome Outcome, string? Error = null)
{
    /// <summary>Успешная доставка.</summary>
    public static NotificationDelivery Delivered { get; } = new(NotificationOutcome.Delivered);

    /// <summary>Временный сбой с описанием.</summary>
    public static NotificationDelivery Transient(string error) => new(NotificationOutcome.Transient, error);

    /// <summary>Постоянная ошибка с описанием.</summary>
    public static NotificationDelivery Permanent(string error) => new(NotificationOutcome.Permanent, error);

    /// <summary>Сообщение доставлено.</summary>
    public bool IsDelivered => Outcome == NotificationOutcome.Delivered;
}
