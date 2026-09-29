using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>
/// Фильтр открытия по фандингу: считается чистый фандинг пары за интервал — рейт шорта минус
/// рейт лонга (позиция платит положительный рейт по лонгу и отрицательный по шорту). Если он
/// строго ниже Arbitrage:MinFundingRatePercent — сделка пропускается целиком (обе ноги одной парой).
/// Нет данных хотя бы по одной ноге — fail-open (сделка разрешена).
/// </summary>
public class FundingFilterTests
{
    private const string Symbol = "BTC/USDT:USDT";
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static (ArbitrageScanner Scanner, FakeConnector Long, FakeConnector Short, FakeExecutor Executor, FakeNotifier Notifier, BotOptions Options) Create()
    {
        var options = new BotOptions();
        options.General.WriteLogFile = false;
        options.Arbitrage.Enabled = true;
        options.Arbitrage.MinSpreadPercentUp = 0.4m;
        options.Arbitrage.SlippageBufferPercent = 0m;
        var config = new FakeConfigProvider(options);

        // спред: ask дешёвой 100 → bid дорогой 101, комиссии круга 0.1×4 → нетто 0.6% > порога 0.4%
        var longConnector = new FakeConnector("binanceusdm");
        longConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            [Symbol] = TestTickers.Make("binanceusdm", Symbol, bid: 99.9m, ask: 100m, ts: Now),
        };

        var shortConnector = new FakeConnector("bybit");
        shortConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            [Symbol] = TestTickers.Make("bybit", Symbol, bid: 101m, ask: 101.1m, ts: Now),
        };

        var registry = new ConnectorRegistry();
        registry.Replace([longConnector, shortConnector]);

        var time = new FakeTimeProvider(Now);
        var executor = new FakeExecutor();
        var notifier = new FakeNotifier();
        var scanner = new ArbitrageScanner(
            config,
            registry,
            new SpreadCalculator(config, time),
            new SymbolFilter(config),
            executor,
            new StatisticsCollector(config, time),
            notifier,
            new EventLog(config, time),
            time,
            NullLogger<ArbitrageScanner>.Instance);

        return (scanner, longConnector, shortConnector, executor, notifier, options);
    }

    [Fact]
    public void Default_limit_is_minus_one_percent()
    {
        Assert.Equal(-1m, new ArbitrageOptions().MinFundingRatePercent);
    }

    [Fact]
    public async Task Pair_income_above_limit_passes_and_funding_is_on_legs()
    {
        // лонг при рейте −0.5 % получает 0.5 %, шорт при рейте −0.2 % платит 0.2 %:
        // чистый доход пары = −0.2 − (−0.5) = +0.3 %, выше предела −1 % — сделка разрешена
        var (scanner, longConnector, shortConnector, executor, notifier, _) = Create();
        longConnector.FundingRatesPercent[Symbol] = -0.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.2m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        var candidate = Assert.Single(executor.Processed);
        Assert.Equal(Symbol, candidate.Symbol);
        Assert.Equal(-0.5m, candidate.LongLeg.FundingPercent);
        Assert.Equal(-0.2m, candidate.ShortLeg.FundingPercent);
    }

    [Fact]
    public async Task Pair_paying_over_limit_whole_pair_skipped()
    {
        // лонг платит +1.5 %, шорт при −0.2 % платит ещё 0.2 %: чистый доход пары −1.7 %,
        // что ниже предела −1 % — фандинг съедает спред, сделка не открывается вовсе
        var (scanner, longConnector, shortConnector, executor, notifier, _) = Create();
        longConnector.FundingRatesPercent[Symbol] = 1.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.2m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Empty(executor.Processed);
    }

    [Fact]
    public async Task Deep_negative_funding_on_short_leg_is_income_not_a_reason_to_skip()
    {
        // прежняя трактовка («провальная» нога — рейт ниже предела) отбрасывала эту связку из-за
        // −1.5 % по лонгу, хотя как раз лонг с отрицательным рейтом нам и платит: доход пары +1.0 %
        var (scanner, longConnector, shortConnector, executor, notifier, _) = Create();
        longConnector.FundingRatesPercent[Symbol] = -1.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.5m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Single(executor.Processed);
    }

    [Fact]
    public async Task Skew_between_legs_is_skipped_although_each_rate_looks_normal()
    {
        // по каждой ноге рейты «в норме» (оба выше −1 %), но платить мы должны 1.1 % за интервал —
        // раньше такой связки фильтр не видел
        var (scanner, longConnector, shortConnector, executor, notifier, _) = Create();
        longConnector.FundingRatesPercent[Symbol] = 0.9m;
        shortConnector.FundingRatesPercent[Symbol] = -0.2m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Empty(executor.Processed);
    }

    [Fact]
    public async Task Missing_funding_fails_open()
    {
        var (scanner, _, _, executor, notifier, _) = Create();
        // FundingRatesPercent пуст у обеих бирж — данных нет, сделку не блокируем

        await scanner.ScanOnceAsync(CancellationToken.None);

        var candidate = Assert.Single(executor.Processed);
        Assert.Null(candidate.LongLeg.FundingPercent);
        Assert.Null(candidate.ShortLeg.FundingPercent);
    }

    [Fact]
    public async Task Funding_endpoint_failure_fails_open()
    {
        var (scanner, longConnector, shortConnector, executor, notifier, _) = Create();
        shortConnector.FailFetchFundingRates = true;
        longConnector.FundingRatesPercent[Symbol] = -0.5m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        var candidate = Assert.Single(executor.Processed);
        Assert.Null(candidate.ShortLeg.FundingPercent);
        Assert.Equal(-0.5m, candidate.LongLeg.FundingPercent);
    }

    [Fact]
    public async Task Filtered_candidate_is_also_handed_to_notifier()
    {
        var (scanner, longConnector, shortConnector, _, notifier, options) = Create();
        options.Notifications.Enabled = true;
        options.Notifications.BotToken = "token";
        options.Notifications.AdminChatId = "42";
        longConnector.FundingRatesPercent[Symbol] = -0.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.2m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Single(notifier.Notified);
    }

    [Fact]
    public async Task Rejected_by_funding_candidate_is_not_notified()
    {
        var (scanner, longConnector, shortConnector, _, notifier, options) = Create();
        options.Notifications.Enabled = true;
        options.Notifications.BotToken = "token";
        options.Notifications.AdminChatId = "42";
        longConnector.FundingRatesPercent[Symbol] = 1.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.2m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Empty(notifier.Notified);
    }

    [Fact]
    public async Task Disabled_notifications_section_notifies_nothing()
    {
        var (scanner, longConnector, shortConnector, _, notifier, _) = Create();
        longConnector.FundingRatesPercent[Symbol] = -0.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.2m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Empty(notifier.Notified);
    }

    [Fact]
    public async Task Rate_equal_to_limit_passes_strictly_below_rejects()
    {
        var (scanner, longConnector, shortConnector, executor, notifier, _) = Create();
        longConnector.FundingRatesPercent[Symbol] = 1m;
        shortConnector.FundingRatesPercent[Symbol] = 0m; // чистый доход пары ровно −1 % — предел, не «ниже»

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Single(executor.Processed);
    }

    [Fact]
    public async Task Configurable_limit_is_respected()
    {
        var (scanner, longConnector, shortConnector, executor, notifier, options) = Create();
        options.Arbitrage.MinFundingRatePercent = 0m; // связка не должна платить фандинг вовсе
        longConnector.FundingRatesPercent[Symbol] = 0.1m;
        shortConnector.FundingRatesPercent[Symbol] = -0.001m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Empty(executor.Processed);
    }

    [Fact]
    public async Task Legs_with_too_different_timestamps_do_not_make_a_signal()
    {
        // котировка дешёвой биржи на 20 с «старше» котировки дорогой: обе проходят общий фильтр
        // возраста, но сравнивать их уже нельзя — большую часть спреда мог дать сдвиг рынка за лаг
        var (scanner, longConnector, shortConnector, executor, _, options) = Create();
        options.Symbols.MaxTickerAgeSeconds = 600;
        options.Symbols.MaxTickerSkewSeconds = 5;
        longConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            [Symbol] = TestTickers.Make("binanceusdm", Symbol, bid: 99.9m, ask: 100m, ts: Now - TimeSpan.FromSeconds(20)),
        };

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Empty(executor.Processed);
    }

    [Fact]
    public async Task Legs_within_skew_limit_still_make_a_signal()
    {
        var (scanner, longConnector, shortConnector, executor, _, options) = Create();
        options.Symbols.MaxTickerAgeSeconds = 600;
        options.Symbols.MaxTickerSkewSeconds = 30;
        longConnector.Tickers = new Dictionary<string, TickerSnapshot>
        {
            [Symbol] = TestTickers.Make("binanceusdm", Symbol, bid: 99.9m, ask: 100m, ts: Now - TimeSpan.FromSeconds(20)),
        };

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Single(executor.Processed);
    }
}
