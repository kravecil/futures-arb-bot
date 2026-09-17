using FuturesArbBot.Infrastructure.Configuration;

namespace FuturesArbBot.Tests;

/// <summary>
/// Загрузка необязательного раздела уведомлений: config/notifications.json, значения по умолчанию
/// при отсутствии файла и переопределение секрета переменной окружения ARB_Notifications__BotToken.
/// </summary>
public sealed class NotificationConfigTests : IDisposable
{
    private const string AppSettings = """{ "General": { "NetworkMode": "DryRun" }, "Arbitrage": { "MinSpreadPercentUp": 0.5 } }""";
    private const string Exchanges = """{ "Exchanges": { "Items": [] } }""";
    private const string Symbols = """{ "Symbols": { "QuoteCurrencies": ["USDT"], "MaxSymbols": 400 } }""";

    private readonly string _directory = Path.Combine(Path.GetTempPath(), "arb-config-" + Guid.NewGuid().ToString("N"));

    public void Dispose()
    {
        try
        {
            Directory.Delete(_directory, recursive: true);
        }
        catch (IOException)
        {
            // временный каталог уже удалён — не важно
        }
    }

    private void WriteFiles(string? notifications)
    {
        Directory.CreateDirectory(_directory);
        File.WriteAllText(Path.Combine(_directory, "appsettings.json"), AppSettings);
        File.WriteAllText(Path.Combine(_directory, "exchanges.json"), Exchanges);
        File.WriteAllText(Path.Combine(_directory, "symbols.json"), Symbols);
        if (notifications is not null)
        {
            File.WriteAllText(Path.Combine(_directory, "notifications.json"), notifications);
        }
    }

    private NotificationOptions Load()
    {
        var (root, directory) = ConfigBootstrapper.Load(_directory);
        return new ConfigProvider(root, directory).Current.Notifications;
    }

    [Fact]
    public void Missing_file_means_disabled_section()
    {
        WriteFiles(notifications: null);

        var options = Load();

        Assert.False(options.Enabled);
        Assert.False(options.IsUsable);
        Assert.Equal(NotificationOptions.DefaultBaseUrl, options.ApiBaseUrl);
        Assert.Equal(MaxSendEndpoint.Messages, options.Endpoint);
        Assert.Null(options.MinSpreadPercent);
        Assert.Equal(120, options.SignalCooldownSeconds);
    }

    [Fact]
    public void Section_is_bound_from_json()
    {
        WriteFiles("""
            {
              "Notifications": {
                "Enabled": true,
                "BotToken": "json.token",
                "AdminChatId": "777",
                "Endpoint": "ChatsMessage",
                "MinSpreadPercent": 0.75,
                "UseMarkdown": true,
                "Silent": true,
                "SignalCooldownSeconds": 30,
                "MaxPerMinute": 5,
                "MaxRetries": 1,
                "FailureCircuitLimit": 3,
                "CircuitBreakMinutes": 15,
                "TextTemplate": "{symbol} {net}"
              }
            }
            """);

        var options = Load();

        Assert.True(options.IsUsable);
        Assert.Equal("json.token", options.BotToken);
        Assert.Equal("777", options.AdminChatId);
        Assert.Equal(MaxSendEndpoint.ChatsMessage, options.Endpoint);
        Assert.Equal(0.75m, options.MinSpreadPercent);
        Assert.True(options.UseMarkdown);
        Assert.True(options.Silent);
        Assert.Equal(30, options.SignalCooldownSeconds);
        Assert.Equal(5, options.MaxPerMinute);
        Assert.Equal(15, options.CircuitBreakMinutes);
        Assert.Equal("{symbol} {net}", options.TextTemplate);
    }

    [Fact]
    public void Environment_variable_overrides_token()
    {
        WriteFiles("""{ "Notifications": { "Enabled": true, "BotToken": "from-file", "AdminChatId": "1" } }""");
        Environment.SetEnvironmentVariable("ARB_Notifications__BotToken", "from-env");
        try
        {
            Assert.Equal("from-env", Load().BotToken);
        }
        finally
        {
            Environment.SetEnvironmentVariable("ARB_Notifications__BotToken", null);
        }
    }
}
