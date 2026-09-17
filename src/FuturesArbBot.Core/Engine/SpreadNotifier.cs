using System.Threading.Channels;

namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Неблокирующий отправитель уведомлений об арбитражных сигналах.
/// <para>
/// <see cref="Notify"/> лишь кладёт готовый текст в ограниченную очередь и немедленно
/// возвращается: цикл сканирования никогда не ждёт сеть. Фоновая читатель выгребает очередь
/// в одну нить, а транспорт сам выполняет повторы.
/// </para>
/// <para>
/// От отправки защищают три фильтра: порог спреда, кулдаун на символ и лимит в минуту.
/// После серии сбоев канал «закрывается» на <c>CircuitBreakMinutes</c> — робот не долбит
/// мессенджер невалидным токеном; при <see cref="NotificationOutcome.Permanent"/> это происходит сразу.
/// </para>
/// </summary>
public sealed class SpreadNotifier(
    IConfigProvider config,
    INotificationTransport transport,
    IEventLog log,
    TimeProvider time,
    ILogger<SpreadNotifier> logger) : ISpreadNotifier, IDisposable
{
    /// <summary>Сколько записей о кулдауне держим, прежде чем почистить устаревшие.</summary>
    private const int CooldownCacheLimit = 512;

    private readonly Lock _gate = new();
    private readonly Dictionary<string, DateTimeOffset> _lastSentBySymbol = new(StringComparer.Ordinal);
    private readonly Queue<DateTimeOffset> _recentSends = new();
    private readonly CancellationTokenSource _cts = new();
    private readonly Channel<string> _queue = Channel.CreateBounded<string>(
        new BoundedChannelOptions(Math.Clamp(config.Current.Notifications.QueueCapacity, 8, 1024))
        {
            FullMode = BoundedChannelFullMode.Wait,
            SingleReader = true,
            SingleWriter = false,
        });

    private int _pumpStarted;
    private Task? _pump;
    private int _consecutiveFailures;
    private DateTimeOffset _circuitUntil;
    private long _suppressed;

    /// <summary>Число сообщений, ожидающих отправки (для диагностики и тестов).</summary>
    public int PendingCount => _queue.Reader.Count;

    /// <summary>Число сигналов, отброшенных фильтрами или переполненной очередью.</summary>
    public long SuppressedCount => Volatile.Read(ref _suppressed);

    public void Notify(SpreadEstimate estimate)
    {
        var options = config.Current;
        var notifications = options.Notifications;
        if (!notifications.IsUsable)
        {
            return;
        }

        var threshold = notifications.MinSpreadPercent ?? options.Arbitrage.MinSpreadPercentUp;
        if (estimate.NetPercent < threshold)
        {
            return;
        }

        var now = time.GetUtcNow();
        string text;
        lock (_gate)
        {
            if (_circuitUntil != default && now < _circuitUntil)
            {
                Suppress();
                return;
            }

            if (InCooldown(estimate.Symbol, now, notifications.SignalCooldownSeconds))
            {
                Suppress();
                return;
            }

            if (RateLimited(now, notifications.MaxPerMinute))
            {
                Suppress();
                log.Debug($"Уведомления: превышен лимит {notifications.MaxPerMinute}/мин — сигнал {estimate.Symbol} пропущен");
                return;
            }

            _lastSentBySymbol[estimate.Symbol] = now;
            _recentSends.Enqueue(now);
            text = NotificationFormatter.Format(estimate, notifications);
        }

        StartPump();
        if (!_queue.Writer.TryWrite(text))
        {
            Suppress();
            log.Warning($"Очередь уведомлений переполнена — сигнал {estimate.Symbol} пропущен");
        }
    }

    /// <summary>Остановить отправитель: очередь закрывается и недолго досылается, процесс не зависает.</summary>
    public void Dispose()
    {
        _queue.Writer.TryComplete();

        try
        {
            _pump?.Wait(TimeSpan.FromSeconds(2));
        }
        catch (Exception)
        {
            // остановка не должна ронять процесс — очередь всё равно закрыта
        }

        if (_queue.Reader.Count > 0)
        {
            log.Debug($">{_queue.Reader.Count} уведомлений не отправлены: сеанс закрыт раньше, чем опустела очередь");
        }

        _cts.Cancel();
        _cts.Dispose();
    }

    /// <summary>Фоновая читатель запускается ровно один раз — при первой постановке в очередь.</summary>
    private void StartPump()
    {
        if (Interlocked.CompareExchange(ref _pumpStarted, 1, 0) != 0)
        {
            return;
        }

        _pump = Task.Run(RunPumpAsync);
    }

    private async Task RunPumpAsync()
    {
        var ct = _cts.Token;
        try
        {
            await foreach (var text in _queue.Reader.ReadAllAsync(ct))
            {
                var notifications = config.Current.Notifications;
                if (IsCircuitOpen(time.GetUtcNow()))
                {
                    log.Debug("Уведомления приостановлены после серии сбоев — сообщение отброшено");
                    continue;
                }

                var result = await transport.SendAsync(text, ct);
                if (result.IsDelivered)
                {
                    Interlocked.Exchange(ref _consecutiveFailures, 0);
                    continue;
                }

                var failures = Interlocked.Increment(ref _consecutiveFailures);
                log.Error($"Уведомление не доставлено ({result.Outcome}): {result.Error}");
                if (result.Outcome == NotificationOutcome.Permanent || failures >= notifications.FailureCircuitLimit)
                {
                    OpenCircuit(time.GetUtcNow(), failures, notifications.CircuitBreakMinutes, result.Outcome);
                }
            }
        }
        catch (OperationCanceledException)
        {
            // штатная остановка
        }
        catch (Exception ex)
        {
            log.Error($"Отправитель уведомлений остановлен: {ex.Message}");
            logger.LogWarning(ex, "notifier pump fault");
        }
    }

    /// <summary>Отправлен ли уже сигнал по этому символу внутри кулдауна.</summary>
    private bool InCooldown(string symbol, DateTimeOffset now, int cooldownSeconds)
    {
        if (cooldownSeconds <= 0)
        {
            return false;
        }

        if (_lastSentBySymbol.TryGetValue(symbol, out var last) && now - last < TimeSpan.FromSeconds(cooldownSeconds))
        {
            return true;
        }

        if (_lastSentBySymbol.Count > CooldownCacheLimit)
        {
            var cutoff = now - TimeSpan.FromSeconds(cooldownSeconds);
            foreach (var stale in _lastSentBySymbol.Where(pair => pair.Value < cutoff).Select(pair => pair.Key).ToList())
            {
                _lastSentBySymbol.Remove(stale);
            }
        }

        return false;
    }

    /// <summary>Выбрано ли скользящее окно «не более N сообщений в минуту».</summary>
    private bool RateLimited(DateTimeOffset now, int maxPerMinute)
    {
        var window = TimeSpan.FromMinutes(1);
        while (_recentSends.Count > 0 && now - _recentSends.Peek() >= window)
        {
            _recentSends.Dequeue();
        }

        return _recentSends.Count >= maxPerMinute;
    }

    private bool IsCircuitOpen(DateTimeOffset now)
    {
        lock (_gate)
        {
            return _circuitUntil != default && now < _circuitUntil;
        }
    }

    /// <summary>Закрыть канал на время: серия сбоев либо заведомо неисправная конфигурация.</summary>
    private void OpenCircuit(DateTimeOffset now, int failures, int breakMinutes, NotificationOutcome outcome)
    {
        lock (_gate)
        {
            if (_circuitUntil != default && now < _circuitUntil)
            {
                return;
            }

            _circuitUntil = now + TimeSpan.FromMinutes(breakMinutes);
        }

        Interlocked.Exchange(ref _consecutiveFailures, 0);
        var reason = outcome == NotificationOutcome.Permanent
            ? "постоянная ошибка — проверьте BotToken и AdminChatId"
            : $"{failures} неудачных отправок подряд";
        log.Warning($"Уведомления приостановлены на {breakMinutes} мин: {reason}");
        logger.LogWarning("notifications circuit opened for {Minutes} min ({Reason})", breakMinutes, reason);
    }

    private void Suppress() => Interlocked.Increment(ref _suppressed);
}

