using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace FuturesArbBot.Infrastructure.Notifications;

/// <summary>Информация о боте из <c>GET /me</c>.</summary>
public sealed record MaxBotInfo(string? Name, string? Username, string? UserId);

/// <summary>Итог проверки токена: успех плюс человекочитаемое описание для консоли.</summary>
public sealed record MaxVerifyResult(bool Success, string Detail, MaxBotInfo? Bot = null);

/// <summary>Диалог, замеченный в апдейтах MAX (для поиска chat_id администратора).</summary>
public sealed record MaxChatHit(string ChatId, string? UpdateType, string? Text);

/// <summary>Страница long-poll <c>GET /updates</c>: маркер для следующего запроса и замеченные чаты.</summary>
public sealed record MaxUpdatesPage(string? Marker, IReadOnlyList<MaxChatHit> Chats);

/// <summary>
/// Транспорт уведомлений: официальный MAX Bot API v2 (по умолчанию https://platform-api2.max.ru).
/// <para>
/// Авторизация — заголовок <c>Authorization: &lt;токен&gt;</c> без префикса Bearer.
/// Отправка — <c>POST /messages?chat_id=…</c> с телом <c>{ text, format?, notify? }</c>;
/// запасной путь <c>POST /chats/{chat_id}/message</c> выбирается настройкой <c>Endpoint</c>.
/// </para>
/// <para>
/// Повторы (429/5xx/таймаут) выполняются здесь же с экспоненциальной задержкой и уважением
/// <c>Retry-After</c>; ошибки 400/401/403/404 считаются постоянными и не повторяются.
/// </para>
/// </summary>
public sealed class MaxMessengerClient : INotificationTransport, IDisposable
{
    /// <summary>Больше этого значения пауза по Retry-After не ждёт — очередь не должна вставать надолго.</summary>
    private static readonly TimeSpan MaxRetryAfter = TimeSpan.FromSeconds(30);

    private static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly IConfigProvider _config;
    private readonly TimeProvider _time;
    private readonly ILogger<MaxMessengerClient> _logger;
    private readonly HttpMessageInvoker _http;

    public MaxMessengerClient(IConfigProvider config, TimeProvider time, ILogger<MaxMessengerClient> logger)
        : this(config, time, logger, handler: null)
    {
    }

    /// <summary>Конструктор для тестов: собственный <see cref="HttpMessageHandler"/>.</summary>
    public MaxMessengerClient(IConfigProvider config, TimeProvider time, ILogger<MaxMessengerClient> logger, HttpMessageHandler? handler)
    {
        _config = config;
        _time = time;
        _logger = logger;

        if (handler is not null)
        {
            _http = new HttpMessageInvoker(handler, disposeHandler: false);
        }
        else
        {
            _http = new HttpMessageInvoker(
                new SocketsHttpHandler
                {
                    PooledConnectionLifetime = TimeSpan.FromMinutes(10),
                    ConnectTimeout = TimeSpan.FromSeconds(5),
                },
                disposeHandler: true);
        }
    }

    public void Dispose() => _http.Dispose();

    /// <inheritdoc />
    public async Task<NotificationDelivery> SendAsync(string text, CancellationToken ct = default)
    {
        var options = _config.Current.Notifications;
        if (string.IsNullOrWhiteSpace(options.BotToken))
        {
            return NotificationDelivery.Permanent("Notifications:BotToken не задан");
        }

        if (string.IsNullOrWhiteSpace(options.AdminChatId))
        {
            return NotificationDelivery.Permanent("Notifications:AdminChatId не задан (запустите --notify-chat-id)");
        }

        NotificationDelivery last = NotificationDelivery.Transient("отправка не выполнялась");
        var attempts = Math.Clamp(options.MaxRetries, 0, 10) + 1;

        for (var attempt = 1; attempt <= attempts; attempt++)
        {
            ct.ThrowIfCancellationRequested();
            var result = await TrySendAsync(options, text, ct);
            last = result.Delivery;

            if (last.IsDelivered || last.Outcome == NotificationOutcome.Permanent)
            {
                return last;
            }

            _logger.LogWarning("MAX Messenger: попытка {Attempt}/{Attempts} не удалась: {Error}", attempt, attempts, last.Error);
            if (attempt == attempts)
            {
                break;
            }

            await DelayAsync(result.RetryAfter ?? Backoff(attempt), ct);
        }

        return last;
    }

    /// <summary>Одна попытка доставки вместе с подсказкой, сколько ждать до следующей.</summary>
    private sealed record Attempt(NotificationDelivery Delivery, TimeSpan? RetryAfter = null);

    private async Task<Attempt> TrySendAsync(NotificationOptions options, string text, CancellationToken ct)
    {
        using var request = BuildRequest(options, text);
        using var timeout = Link(options.TimeoutMs, ct);

        try
        {
            using var response = await _http.SendAsync(request, timeout.Token);
            var body = await ReadBodyAsync(response, timeout.Token);
            if (response.IsSuccessStatusCode)
            {
                return new Attempt(NotificationDelivery.Delivered);
            }

            return new Attempt(Classify(response, body), RetryAfter(response));
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw; // остановка процесса — не наша ошибка
        }
        catch (OperationCanceledException)
        {
            return new Attempt(NotificationDelivery.Transient($"таймаут {options.TimeoutMs} мс"));
        }
        catch (Exception ex)
        {
            return new Attempt(NotificationDelivery.Transient($"сеть: {ex.Message}"));
        }
    }
    /// <summary>Проверка токена (<c>GET /me</c>) — используется режимом --notify-test.</summary>
    public async Task<MaxVerifyResult> VerifyAsync(CancellationToken ct = default)
    {
        var options = _config.Current.Notifications;
        if (string.IsNullOrWhiteSpace(options.BotToken))
        {
            return new MaxVerifyResult(false, "Notifications:BotToken не задан");
        }

        using var request = new HttpRequestMessage(HttpMethod.Get, BuildUrl(options, "me"));
        Authorize(request, options.BotToken);
        using var timeout = Link(Math.Max(options.TimeoutMs, 5_000), ct);

        try
        {
            using var response = await _http.SendAsync(request, timeout.Token);
            var body = await ReadBodyAsync(response, timeout.Token);
            if (!response.IsSuccessStatusCode)
            {
                var (code, message) = ParseError(body);
                return new MaxVerifyResult(false, $"{(int)response.StatusCode} {code} {message}".Trim());
            }

            var bot = ParseBot(body);
            return new MaxVerifyResult(true, $"токен принят, бот «{bot.Name ?? bot.Username ?? "?"}»", bot);
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            return new MaxVerifyResult(false, $"таймаут {options.TimeoutMs} мс");
        }
        catch (Exception ex)
        {
            return new MaxVerifyResult(false, ex.Message);
        }
    }

    /// <summary>
    /// Одна длинная подписка <c>GET /updates</c>: вернуть замеченные chat_id.
    /// Так бот узнаёт адресата: первым сообщение пишет администратор, робот его только читает.
    /// </summary>
    public async Task<MaxUpdatesPage> PollUpdatesAsync(string? marker, CancellationToken ct = default)
    {
        var options = _config.Current.Notifications;
        var query = $"updates?limit=50&timeout={Math.Clamp(options.TimeoutMs, 500, 60_000)}";
        if (!string.IsNullOrWhiteSpace(marker))
        {
            query += $"&marker={Uri.EscapeDataString(marker)}";
        }

        using var request = new HttpRequestMessage(HttpMethod.Get, BuildUrl(options, query));
        Authorize(request, options.BotToken);
        using var timeout = Link(options.TimeoutMs + 5_000, ct);

        using var response = await _http.SendAsync(request, timeout.Token);
        var body = await ReadBodyAsync(response, timeout.Token);
        if (!response.IsSuccessStatusCode)
        {
            var (code, message) = ParseError(body);
            throw new HttpRequestException($"updates: {(int)response.StatusCode} {code} {message}".Trim());
        }

        return ParseUpdates(body);
    }

    // ----------------------------- HTTP и разбор ответа -----------------------------

    /// <summary>Собрать запрос отправки: путь зависит от настройки Endpoint, тело — текст + формат + notify.</summary>
    private static HttpRequestMessage BuildRequest(NotificationOptions options, string text)
    {
        var chatId = Uri.EscapeDataString(options.AdminChatId.Trim());
        var path = options.Endpoint == MaxSendEndpoint.ChatsMessage
            ? $"chats/{chatId}/message"
            : $"messages?chat_id={chatId}&disable_link_preview=true";

        var request = new HttpRequestMessage(HttpMethod.Post, BuildUrl(options, path));
        Authorize(request, options.BotToken);

        var payload = new { text, format = options.UseMarkdown ? "markdown" : null, notify = options.Silent ? false : (bool?)null };
        request.Content = new StringContent(JsonSerializer.Serialize(payload, JsonOptions), Encoding.UTF8, "application/json");
        return request;
    }

    /// <summary>Собрать абсолютный URL: базовый адрес без завершающего слэша + относительный путь.</summary>
    private static Uri BuildUrl(NotificationOptions options, string path) =>
        new(new Uri(options.ApiBaseUrl.Trim().TrimEnd('/') + "/"), path);

    /// <summary>MAX ждёт токен в заголовке без схемы Bearer.</summary>
    private static void Authorize(HttpRequestMessage request, string token) =>
        request.Headers.TryAddWithoutValidation("Authorization", token.Trim());

    private static CancellationTokenSource Link(int timeoutMs, CancellationToken ct)
    {
        var linked = CancellationTokenSource.CreateLinkedTokenSource(ct);
        linked.CancelAfter(Math.Clamp(timeoutMs, 500, 120_000));
        return linked;
    }

    private static async Task<string> ReadBodyAsync(HttpResponseMessage response, CancellationToken ct)
    {
        try
        {
            return await response.Content.ReadAsStringAsync(ct);
        }
        catch (Exception)
        {
            return string.Empty; // тело необязательно: статус важнее
        }
    }

    /// <summary>Постоянные ошибки (4xx кроме 429) не повторяем, временные — да.</summary>
    private static NotificationDelivery Classify(HttpResponseMessage response, string body)
    {
        var (code, message) = ParseError(body);
        var status = (int)response.StatusCode;
        var description = $"{status} {code} {message}".Trim();

        return status switch
        {
            400 or 401 or 403 or 404 => NotificationDelivery.Permanent(description),
            429 => NotificationDelivery.Transient($"лимит запросов: {description}"),
            >= 500 => NotificationDelivery.Transient(description),
            _ => NotificationDelivery.Permanent(description),
        };
    }

    /// <summary>Поле <c>Retry-After</c> (секунды или дата) — сколько ждать перед повтором.</summary>
    private TimeSpan? RetryAfter(HttpResponseMessage response)
    {
        if (response.Headers.RetryAfter is not { } hint)
        {
            return null;
        }

        var delay = hint.Delta ?? (hint.Date is { } date ? date - _time.GetUtcNow() : null);
        return delay is null ? null : TimeSpan.FromTicks(Math.Clamp(delay.Value.Ticks, 0, MaxRetryAfter.Ticks));
    }

    /// <summary>Код и текст ошибки MAX: поля <c>code</c>/<c>message</c> лежат в корне или в <c>meta</c>.</summary>
    private static (string? Code, string? Message) ParseError(string body)
    {
        if (string.IsNullOrWhiteSpace(body))
        {
            return (null, null);
        }

        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            var source = root.ValueKind == JsonValueKind.Object && root.TryGetProperty("meta", out var meta) && meta.ValueKind == JsonValueKind.Object
                ? meta
                : root;

            var preview = Truncate(body);
            return (ReadString(source, "code"), ReadString(source, "message") ?? ReadString(source, "description") ?? preview);
        }
        catch (JsonException)
        {
            return (null, Truncate(body));
        }
    }

    private static string Truncate(string text) => text.Length > 300 ? text[..300] + "…" : text;


    private static MaxBotInfo ParseBot(string body)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("bot", out var nested) && nested.ValueKind == JsonValueKind.Object)
            {
                root = nested;
            }

            return new MaxBotInfo(ReadString(root, "name"), ReadString(root, "username"), ReadString(root, "user_id"));
        }
        catch (JsonException)
        {
            return new MaxBotInfo(null, null, null);
        }
    }

    /// <summary>
    /// Разбор <c>GET /updates</c>: собираем любые <c>chat_id</c> рекурсивно — форма апдейта
    /// различается у message_created, bot_started и остальных типов событий.
    /// </summary>
    private static MaxUpdatesPage ParseUpdates(string body)
    {
        List<MaxChatHit> hits = [];
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            var marker = ReadString(root, "marker");
            if (!root.TryGetProperty("updates", out var updates) || updates.ValueKind != JsonValueKind.Array)
            {
                return new MaxUpdatesPage(marker, hits);
            }

            foreach (var update in updates.EnumerateArray())
            {
                var type = ReadString(update, "update_type");
                var text = FindString(update, "text");
                foreach (var chatId in FindChatIds(update))
                {
                    hits.Add(new MaxChatHit(chatId, type, text));
                }
            }

            return new MaxUpdatesPage(marker, hits);
        }
        catch (JsonException)
        {
            return new MaxUpdatesPage(null, hits);
        }
    }

    private static string? ReadString(JsonElement element, string property) =>
        element.ValueKind == JsonValueKind.Object
        && element.TryGetProperty(property, out var value)
        && value.ValueKind is JsonValueKind.String or JsonValueKind.Number
        && !string.IsNullOrWhiteSpace(value.ToString())
            ? value.ToString()
            : null;

    /// <summary>Первое попавшееся строковое поле с указанным именем (нужно для текста входящего сообщения).</summary>
    private static string? FindString(JsonElement element, string property)
    {
        if (element.ValueKind == JsonValueKind.Object)
        {
            foreach (var child in element.EnumerateObject())
            {
                if (child.NameEquals(property) && child.Value.ValueKind == JsonValueKind.String && !string.IsNullOrWhiteSpace(child.Value.GetString()))
                {
                    return child.Value.GetString();
                }
            }

            foreach (var child in element.EnumerateObject())
            {
                if (FindString(child.Value, property) is { } found)
                {
                    return found;
                }
            }
        }
        else if (element.ValueKind == JsonValueKind.Array)
        {
            foreach (var item in element.EnumerateArray())
            {
                if (FindString(item, property) is { } found)
                {
                    return found;
                }
            }
        }

        return null;
    }

    private static IEnumerable<string> FindChatIds(JsonElement element)
    {
        List<string> ids = [];
        CollectChatIds(element, ids);
        return ids.Distinct(StringComparer.Ordinal);
    }

    private static void CollectChatIds(JsonElement element, List<string> sink)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                foreach (var child in element.EnumerateObject())
                {
                    if (child.NameEquals("chat_id") && child.Value.ValueKind is JsonValueKind.String or JsonValueKind.Number && ReadString(element, "chat_id") is { } id)
                    {
                        sink.Add(id);
                    }

                    CollectChatIds(child.Value, sink);
                }

                break;

            case JsonValueKind.Array:
                foreach (var item in element.EnumerateArray())
                {
                    CollectChatIds(item, sink);
                }

                break;
        }
    }

    /// <summary>Экспоненциальная пауза между попытками: 0,3с → 0,6с → 1,2с … не дольше 5с.</summary>
    private static TimeSpan Backoff(int attempt) =>
        TimeSpan.FromMilliseconds(Math.Min(5_000, 300 * Math.Pow(2, Math.Max(0, attempt - 1))));

    private async Task DelayAsync(TimeSpan delay, CancellationToken ct)
    {
        try
        {
            await Task.Delay(delay, _time, ct);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // процесс останавливается — пауза прервана, это штатно
        }
    }
}
