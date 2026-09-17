using System.Diagnostics;
using OrderRequest = FuturesArbBot.Core.Domain.OrderRequest;
using OrderResult = FuturesArbBot.Core.Domain.OrderResult;
using OrderSide = FuturesArbBot.Core.Domain.OrderSide;

namespace FuturesArbBot.Infrastructure.Ccxt;

/// <summary>
/// Адаптер одной биржи к интерфейсу <see cref="IExchangeConnector"/>.
/// Работает через официальный C#-порт CCXT (NuGet-пакет ccxt):
/// загрузка рынков (только линейные бессрочные), опрос тикеров, рыночные ордера.
/// </summary>
public sealed class CcxtExchangeConnector : IExchangeConnector
{
    private readonly ExchangeConfigEntry _entry;
    private readonly NetworkMode _mode;
    private readonly IEventLog _log;
    private readonly TimeProvider _time;
    private readonly ILogger _logger;
    private readonly Exchange _api;

    private readonly Dictionary<string, MarketInfo> _markets = new(StringComparer.Ordinal);
    private readonly Dictionary<string, ExchangeFees> _feesBySymbol = new(StringComparer.Ordinal);
    private ExchangeFees _defaultFees = new(0.1m, 0.05m);

    // --- фандинг-рейты: редкий кэш (рейты обновляются раз в 4–8 часов, REST-эндпоинт тяжёлый) ---
    private static readonly TimeSpan FundingCacheTtl = TimeSpan.FromMinutes(5);
    private IReadOnlyDictionary<string, decimal> _fundingCache = new Dictionary<string, decimal>();
    private DateTimeOffset _fundingFetchedAt = DateTimeOffset.MinValue;
    private bool _fundingUnavailableLogged;

    public CcxtExchangeConnector(ExchangeConfigEntry entry, NetworkMode mode, IEventLog log, TimeProvider time, ILogger logger)
    {
        _entry = entry;
        _mode = mode;
        _log = log;
        _time = time;
        _logger = logger;

        var config = new Dictionary<string, object>
        {
            ["enableRateLimit"] = true,
            ["timeout"] = 15000.0,
        };

        var apiKey = FirstNotEmpty(entry.ApiKey, EnvOverride(entry.Id, "API_KEY"), EnvOverride(entry.Id, "APIKEY"));
        var secret = FirstNotEmpty(entry.Secret, EnvOverride(entry.Id, "SECRET"));
        var password = FirstNotEmpty(entry.Password, EnvOverride(entry.Id, "PASSWORD"));
        if (apiKey is not null)
        {
            config["apiKey"] = apiKey;
        }

        if (secret is not null)
        {
            config["secret"] = secret;
        }

        if (password is not null)
        {
            config["password"] = password;
        }

        // биржи, где надо явно указать тип рынка (иначе fetchTickers вернёт спот)
        var options = DefaultOptions(entry.Id);
        if (entry.Options is { Count: > 0 })
        {
            foreach (var (key, value) in entry.Options)
            {
                options[key] = value;
            }
        }

        if (options.Count > 0)
        {
            config["options"] = options;
        }

        _api = CcxtExchangeFactory.Instantiate(entry.Id, config);
    }

    public string Id => _entry.Id;

    public string DisplayName => string.IsNullOrWhiteSpace(_entry.Name) ? _entry.Id : _entry.Name;

    public NetworkMode Mode => _mode;

    public ExchangeFees DefaultFees => _defaultFees;

    public int MarketCount => _markets.Count;

    public IReadOnlyList<MarketInfo> PerpetualMarkets => [.. _markets.Values];

    public async Task ConnectAsync(CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();

        // testnet включается ДО загрузки рынков — у него свои URL и списки инструментов
        if (_mode == NetworkMode.Testnet)
        {
            _api.setSandboxMode(true);
        }

        var markets = await _api.LoadMarkets();
        _markets.Clear();
        _feesBySymbol.Clear();

        foreach (var (symbol, market) in markets)
        {
            if (market.swap != true || market.linear != true || market.active == false)
            {
                continue;
            }

            var takerPct = ToPct(market.taker);
            var makerPct = ToPct(market.maker);

            double? minAmount = null;
            try
            {
                minAmount = market.limits?.amount?.min;
            }
            catch
            {
                // у части бирж limits не заполнен — работаем без ограничений
            }

            _markets[symbol] = new MarketInfo(
                symbol,
                IsPerpetual: true,
                IsLinear: true,
                market.quote ?? market.settle ?? "USDT",
                minAmount is > 0 ? (decimal)minAmount.Value : null,
                takerPct,
                makerPct);

            if (takerPct > 0m)
            {
                _feesBySymbol[symbol] = new ExchangeFees(takerPct, makerPct);
            }
        }

        // комиссия по умолчанию: переопределение из конфига → BTC/USDT:USDT → любой рынок → (0.1 / 0.05)
        var reference = _feesBySymbol.GetValueOrDefault("BTC/USDT:USDT", _feesBySymbol.Values.FirstOrDefault());
        _defaultFees = new ExchangeFees(
            _entry.TakerFeePercent ?? (reference.TakerPercent > 0m ? reference.TakerPercent : 0.1m),
            _entry.MakerFeePercent ?? (reference.MakerPercent > 0m ? reference.MakerPercent : 0.05m));
    }

    public async Task<FetchTickersResult> FetchTickersAsync(CancellationToken ct = default)
    {
        ct.ThrowIfCancellationRequested();

        var stopwatch = Stopwatch.StartNew();
        var raw = await _api.FetchTickers();

        // часть бирж (например, Binance) отдаёт bid/ask отдельным запросом bookTicker — дослияем
        try
        {
            var bidsAsks = await _api.FetchBidsAsks(null, null);
            foreach (var (symbol, bookTicker) in bidsAsks.tickers)
            {
                if (!raw.tickers.TryGetValue(symbol, out var ticker))
                {
                    continue;
                }

                if (bookTicker.bid is { } bid && bid > 0.0)
                {
                    ticker.bid = bid;
                }

                if (bookTicker.ask is { } ask && ask > 0.0)
                {
                    ticker.ask = ask;
                }

                raw.tickers[symbol] = ticker;
            }
        }
        catch (Exception ex) when (ex is NotSupported or OperationFailed or ExchangeError)
        {
            _logger.LogDebug(ex, "FetchBidsAsks недоступен — работаем на bid/ask из fetchTickers");
        }

        var now = _time.GetUtcNow();

        var tickers = raw.tickers;
        var result = new Dictionary<string, TickerSnapshot>(tickers.Count, StringComparer.Ordinal);
        foreach (var (symbol, ticker) in tickers)
        {
            if (!_markets.ContainsKey(symbol))
            {
                continue; // интересуют только наши линейные бессрочные
            }

            if (ticker.bid is not { } bid || ticker.ask is not { } ask || bid <= 0.0 || ask <= 0.0)
            {
                continue;
            }

            var timestamp = ticker.timestamp is { } ms && ms > 0
                ? DateTimeOffset.FromUnixTimeMilliseconds(ms)
                : now;

            var quoteVolume = ticker.quoteVolume ?? 0.0;
            if (quoteVolume <= 0.0 && ticker.info is not null)
            {
                // некоторые биржи (например OKX) не парсят объём в тикер — достаём из сырых данных
                quoteVolume = ExtractQuoteVolume(ticker.info);
            }

            result[symbol] = new TickerSnapshot(
                Id,
                symbol,
                (decimal)bid,
                (decimal)ask,
                (decimal)(ticker.last ?? bid),
                (decimal)quoteVolume,
                timestamp);
        }

        return new FetchTickersResult(result, stopwatch.Elapsed);
    }

    public async Task<OrderResult> PlaceOrderAsync(OrderRequest request, CancellationToken ct = default)
    {
        try
        {
            ct.ThrowIfCancellationRequested();

            var amountObject = _api.amountToPrecision(request.Symbol, (double)request.Amount);
            var amount = Convert.ToDouble(amountObject, CultureInfo.InvariantCulture);
            if (amount <= 0.0)
            {
                return OrderResult.Fail($"объём после округления равен нулю ({request.Amount})");
            }

            // имена и условия параметров биржи (перевод политики заявки) — в OrderParamsBuilder
            var parameters = OrderParamsBuilder.Build(request, ExchangeCapabilityMap.For(Id));

            double? price = null;
            // ChaseLimit на бирже — это limit: локальное догонание делает исполнитель,
            // нативное — сама биржа через параметры из request.ExchangeParams.
            var type = request.Type == OrderType.Market ? "market" : "limit";
            if (request.Type != OrderType.Market)
            {
                if (request.Price is not { } limitPrice || limitPrice <= 0m)
                {
                    return OrderResult.Fail("для лимитного ордера не задана цена");
                }

                price = Convert.ToDouble(_api.priceToPrecision(request.Symbol, (double)limitPrice), CultureInfo.InvariantCulture);
            }

            var side = request.Side == OrderSide.Buy ? "buy" : "sell";
            var order = await _api.CreateOrder(request.Symbol, type, side, amount, price, parameters);

            var filled = order.filled is { } f && f > 0.0 ? (decimal)f : 0m;
            decimal? average = order.average is { } a && a > 0.0 ? (decimal)a
                : order.price is { } p && p > 0.0 ? (decimal)p
                : null;

            // рыночный обязан исполниться сразу; лимитный мог ещё не набрать объёма
            if (request.Type == OrderType.Market && order.status is "open" or "rejected" && filled <= 0m)
            {
                return OrderResult.Fail($"рыночный ордер не исполнен (статус: {order.status ?? "unknown"})");
            }

            return OrderResult.Ok(order.id, average, filled);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            var message = ExplainError(ex);
            _log.Error($"[{DisplayName}] ордер {request.Side} {request.Symbol} × {request.Amount}: {message}");
            _logger.LogDebug(ex, "order failed");
            return OrderResult.Fail(message);
        }
    }

    public async Task<OrderUpdate?> FetchOrderAsync(string orderId, string symbol, CancellationToken ct = default)
    {
        try
        {
            ct.ThrowIfCancellationRequested();
            var order = await _api.FetchOrder(orderId, symbol);

            var filled = order.filled is { } f && f > 0.0 ? (decimal)f : 0m;
            decimal? average = order.average is { } a && a > 0.0 ? (decimal)a : null;

            return new OrderUpdate(orderId, MapStatus(order.status), filled, average);
        }
        catch (OrderNotFound)
        {
            return null;
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogDebug(ex, "FetchOrder {OrderId} {Symbol} failed", orderId, symbol);
            // ошибку опроса не считаем смертью ордера: вернём «открыт» с нулевым исполнением
            return new OrderUpdate(orderId, OrderStatus.Unknown, 0m, null);
        }
    }

    public async Task<bool> CancelOrderAsync(string orderId, string symbol, CancellationToken ct = default)
    {
        try
        {
            ct.ThrowIfCancellationRequested();
            await _api.CancelOrder(orderId, symbol);
            return true;
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogDebug(ex, "CancelOrder {OrderId} {Symbol} failed", orderId, symbol);
            return false;
        }
    }

    public async Task<IReadOnlyList<PositionSnapshot>> FetchPositionsAsync(CancellationToken ct = default)
    {
        // исключения не глотаем: вызывающий код сам решает, что делать при недоступности
        // сверки (reconcile консервативно сохраняет прошлый снимок, закрытие — не «обнуляет» позицию)
        ct.ThrowIfCancellationRequested();
        var positions = await _api.FetchPositions();

        var result = new List<PositionSnapshot>();
        foreach (var position in positions)
        {
            if (position.symbol is not { Length: > 0 } symbol)
            {
                continue;
            }

            var contracts = position.contracts ?? 0.0;
            if (contracts <= 0.0)
            {
                continue; // закрытые позиции биржи отдают нулевыми — экспозиции в них нет
            }

            var side = position.side == "short" ? OrderSide.Sell : OrderSide.Buy;
            decimal? entry = position.entryPrice is { } openPrice && openPrice > 0.0 ? (decimal)openPrice : null;
            result.Add(new PositionSnapshot(symbol, side, (decimal)contracts, entry));
        }

        return result;
    }

    public async Task<IReadOnlyDictionary<string, decimal>> FetchFundingRatesPercentAsync(CancellationToken ct = default)
    {
        var now = _time.GetUtcNow();
        if (now - _fundingFetchedAt < FundingCacheTtl)
        {
            return _fundingCache;
        }

        _fundingFetchedAt = now;
        try
        {
            ct.ThrowIfCancellationRequested();
            var response = await _api.FetchFundingRates();

            var rates = new Dictionary<string, decimal>(_markets.Count, StringComparer.Ordinal);
            foreach (var (symbol, fundingRate) in response.fundingRates)
            {
                if (fundingRate.fundingRate is { } rate && _markets.ContainsKey(symbol))
                {
                    rates[symbol] = (decimal)rate * 100m; // дробь CCXT (0.0001) → проценты (0.01 %)
                }
            }

            _fundingCache = rates;
            _fundingUnavailableLogged = false;
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            // fail-open: без данных фандинга сделки не блокируются — предупредим один раз до первого успеха
            if (!_fundingUnavailableLogged)
            {
                _fundingUnavailableLogged = true;
                _log.Error($"[{DisplayName}] фандинг-рейты недоступны: {ExplainError(ex)} — фильтр по фандингу будет пропускать сделки");
                _logger.LogDebug(ex, "FetchFundingRates failed for {ExchangeId}", Id);
            }
        }

        return _fundingCache;
    }

    private static OrderStatus MapStatus(string? status) => status switch
    {
        "open" or "pending" or "unfilled" or "partially_filled" or "partially-closed" => OrderStatus.Open,
        "closed" or "canceled_filled" or "filled" => OrderStatus.Filled,
        "canceled" => OrderStatus.Canceled,
        "expired" => OrderStatus.Expired,
        "rejected" => OrderStatus.Rejected,
        _ => OrderStatus.Unknown,
    };

    public async Task SetLeverageAsync(int leverage, string symbol, CancellationToken ct = default)
    {
        if (leverage <= 1)
        {
            return;
        }

        try
        {
            await _api.SetLeverage(leverage, symbol);
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _logger.LogDebug(ex, "SetLeverage {Symbol} x{Leverage} — биржа могла не применить плечо", symbol, leverage);
        }
    }

    public async Task<bool> VerifyAccessAsync(CancellationToken ct = default)
    {
        try
        {
            await _api.FetchBalance();
            return true;
        }
        catch (Exception ex) when (ex is not OperationCanceledException)
        {
            _log.Error($"[{DisplayName}] проверка торгового доступа не прошла: {ExplainError(ex)}");
            return false;
        }
    }

    public bool TryGetFees(string symbol, out ExchangeFees fees)
    {
        if (_feesBySymbol.TryGetValue(symbol, out var found) && found.TakerPercent > 0m)
        {
            fees = found;
            return true;
        }

        fees = _defaultFees;
        return false;
    }

    public ValueTask DisposeAsync()
    {
        if (_api is IDisposable disposable)
        {
            disposable.Dispose();
        }

        return ValueTask.CompletedTask;
    }

    // ------------------------- вспомогательное -------------------------

    private static Dictionary<string, object> DefaultOptions(string id) => id.ToLowerInvariant() switch
    {
        "bybit" => new Dictionary<string, object> { ["defaultType"] = "swap", ["defaultSubType"] = "linear" },
        "okx" or "gate" or "mexc" or "bitget" or "htx" or "coinex" or "phemex" => new Dictionary<string, object> { ["defaultType"] = "swap" },
        _ => [],
    };

    private static decimal ToPct(double? fraction) => fraction is > 0.0 ? (decimal)(fraction.Value * 100.0) : 0m;

    private static double ExtractQuoteVolume(Dictionary<string, object> info)
    {
        foreach (var key in new[] { "volCcy24h", "turnover24h", "quoteVolume", "volValue24h" })
        {
            if (!info.TryGetValue(key, out var value))
            {
                continue;
            }

            try
            {
                var volume = Convert.ToDouble(value, CultureInfo.InvariantCulture);
                if (volume > 0.0)
                {
                    return volume;
                }
            }
            catch
            {
                // нечисловое поле — пропускаем
            }
        }

        return 0.0;
    }

    private static string? FirstNotEmpty(params ReadOnlySpan<string?> candidates)
    {
        foreach (var candidate in candidates)
        {
            if (!string.IsNullOrWhiteSpace(candidate))
            {
                return candidate;
            }
        }

        return null;
    }

    private static string? EnvOverride(string id, string suffix) => Environment.GetEnvironmentVariable($"ARB_{id.Replace('-', '_').ToUpperInvariant()}_{suffix}") is { Length: > 0 } value ? value : null;

    private static string ExplainError(Exception ex) => ex switch
    {
        InsufficientFunds => "недостаточно средств на счете",
        RateLimitExceeded or DDoSProtection => "превышен лимит запросов к бирже",
        RequestTimeout => "таймаут запроса",
        ExchangeNotAvailable or ExchangeClosedByUser or OnMaintenance => "биржа недоступна или на обслуживании",
        AuthenticationError => "ошибка аутентификации (проверьте API-ключи)",
        InvalidOrder => "ордер отклонён биржей (объём или цена)",
        BadSymbol => "неизвестный инструмент",
        NetworkError => "сетевая ошибка",
        _ => $"{ex.GetType().Name}: {ex.Message}",
    };
}
