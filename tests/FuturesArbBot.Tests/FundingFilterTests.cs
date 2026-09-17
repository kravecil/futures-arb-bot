using FuturesArbBot.Core.Domain;
using FuturesArbBot.Core.Engine;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>
/// Фильтр открытия по фандингу: если фандинг хотя бы по одной ноге строго ниже
/// Arbitrage:MinFundingRatePercent — сделка пропускается целиком (обе ноги одной парой).
/// Нет данных о фандинге — fail-open (сделка разрешена).
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

        // спред: ask дешёвой 100 → bid дорогой 101, комиссии 0.1+0.1 → нетто ~0.8% > порога 0.4%
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
    public async Task Both_legs_above_limit_candidate_passes_with_funding_on_legs()
    {
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
    public async Task One_leg_below_limit_whole_pair_skipped()
    {
        // из задания: по одной ноге -1.5% (ниже предела -1%), по второй -0.5% (норм) —
        // сделка не открывается ни по одной ноге
        var (scanner, longConnector, shortConnector, executor, notifier, _) = Create();
        longConnector.FundingRatesPercent[Symbol] = -1.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.5m;

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
        longConnector.FundingRatesPercent[Symbol] = -1.5m;
        shortConnector.FundingRatesPercent[Symbol] = -0.5m;

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
        longConnector.FundingRatesPercent[Symbol] = -1m; // ровно предел — не «ниже», пропускаем
        shortConnector.FundingRatesPercent[Symbol] = -0.999m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Single(executor.Processed);
    }

    [Fact]
    public async Task Configurable_limit_is_respected()
    {
        var (scanner, longConnector, shortConnector, executor, notifier, options) = Create();
        options.Arbitrage.MinFundingRatePercent = 0m; // запрет на отрицательный фандинг
        longConnector.FundingRatesPercent[Symbol] = 0.1m;
        shortConnector.FundingRatesPercent[Symbol] = -0.001m;

        await scanner.ScanOnceAsync(CancellationToken.None);

        Assert.Empty(executor.Processed);
    }
}
