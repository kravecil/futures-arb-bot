using Microsoft.Extensions.Configuration;

namespace FuturesArbBot.App.Services;

/// <summary>Сборка контейнера зависимостей приложения.</summary>
public static class Bootstrapper
{
    public static AppRunner Build(CliOptions cli)
    {
        var (configRoot, configDir) = ConfigBootstrapper.Load(cli.ConfigDir);

        var services = new ServiceCollection();
        services.AddSingleton(cli);
        services.AddSingleton(TimeProvider.System);
        services.AddLogging();
        services.AddSingleton<IConfigurationRoot>(configRoot);
        services.AddSingleton<IConfigProvider>(_ => new ConfigProvider(configRoot, configDir));
        services.AddSingleton<IEventLog, EventLog>();
        services.AddSingleton<ConnectorRegistry>();
        services.AddSingleton<IExchangeFactory, CcxtExchangeFactory>();
        services.AddSingleton<ISpreadCalculator, SpreadCalculator>();
        services.AddSingleton<ISymbolFilter, SymbolFilter>();
        services.AddSingleton<ITradeExecutor, ArbTradeExecutor>();
        services.AddSingleton<IStatisticsCollector, StatisticsCollector>();
        services.AddSingleton<IArbitrageScanner, ArbitrageScanner>();
        services.AddSingleton<Dashboard>();
        services.AddSingleton<SessionReportPrinter>();
        services.AddSingleton<AppRunner>();

        var provider = services.BuildServiceProvider();

        // все записи ILogger уходят в журнал событий (дашборд + файл)
        var eventLog = provider.GetRequiredService<IEventLog>();
        provider.GetRequiredService<ILoggerFactory>().AddProvider(new EventLogLoggerProvider(eventLog));

        return provider.GetRequiredService<AppRunner>();
    }
}
