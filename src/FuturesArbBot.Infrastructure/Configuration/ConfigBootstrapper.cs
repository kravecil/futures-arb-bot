using Microsoft.Extensions.Configuration;

namespace FuturesArbBot.Infrastructure.Configuration;

/// <summary>
/// Загрузка конфигурации из JSON-файлов каталога config/:
/// appsettings.json — общие настройки и арбитраж,
/// exchanges.json — список бирж и ключи,
/// symbols.json — фильтры торговых пар,
/// notifications.json — (необязательный) раздел уведомлений администратору.
/// Изменения подхватываются «на лету» (reloadOnChange).
/// </summary>
public static class ConfigBootstrapper
{
    private static readonly string[] Files = ["appsettings.json", "exchanges.json", "symbols.json"];

    /// <summary>Файлы, которых может не быть: разделы берутся из значений по умолчанию.</summary>
    private static readonly string[] OptionalFiles = ["notifications.json"];

    public static (IConfigurationRoot Config, string Directory) Load(string? configuredDirectory)
    {
        var dir = ResolveConfigDirectory(configuredDirectory);

        var builder = new ConfigurationBuilder().SetBasePath(dir);
        foreach (var file in Files)
        {
            builder.AddJsonFile(file, optional: false, reloadOnChange: true);
        }

        foreach (var file in OptionalFiles)
        {
            builder.AddJsonFile(file, optional: true, reloadOnChange: true);
        }

        builder.AddEnvironmentVariables("ARB_");
        return (builder.Build(), dir);
    }

    private static string ResolveConfigDirectory(string? configured)
    {
        if (!string.IsNullOrWhiteSpace(configured))
        {
            if (!Directory.Exists(configured))
            {
                throw new DirectoryNotFoundException($"Каталог конфигурации не найден: {configured}");
            }

            return configured;
        }

        var missing = (string dir) => Files.Where(f => !File.Exists(Path.Combine(dir, f))).ToList();

        // порядок: текущий каталог → config/ → вверх по дереву (для dotnet run из любого места) → рядом с бинарником
        var candidates = new List<string> { Directory.GetCurrentDirectory() };
        for (var dir = Directory.GetCurrentDirectory(); dir is not null; dir = Path.GetDirectoryName(dir))
        {
            candidates.Add(Path.Combine(dir, "config"));
        }

        candidates.Add(AppContext.BaseDirectory);
        candidates.Add(Path.Combine(AppContext.BaseDirectory, "config"));

        foreach (var candidate in candidates)
        {
            if (missing(candidate).Count == 0)
            {
                return candidate;
            }
        }

        throw new FileNotFoundException(
            $"Не найдены файлы конфигурации: {string.Join(", ", Files)}. " +
            $"Запустите робота из каталога проекта или укажите каталог через --config-dir.");
    }
}

/// <summary>Реализация <see cref="IConfigProvider"/> поверх IConfiguration с перечиткой на каждом доступе.</summary>
public sealed class ConfigProvider(IConfiguration configuration, string directory) : IConfigProvider
{
    public BotOptions Current => new()
    {
        General = configuration.GetSection("General").Get<GeneralOptions>() ?? new(),
        Arbitrage = configuration.GetSection("Arbitrage").Get<ArbitrageOptions>() ?? new(),
        Symbols = configuration.GetSection("Symbols").Get<SymbolsOptions>() ?? new(),
        Exchanges = configuration.GetSection("Exchanges").Get<ExchangesOptions>() ?? new(),
        Notifications = configuration.GetSection("Notifications").Get<NotificationOptions>() ?? new(),
    };

    public string ConfigDirectory => directory;
}
