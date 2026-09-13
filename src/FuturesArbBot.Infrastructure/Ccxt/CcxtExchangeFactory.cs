namespace FuturesArbBot.Infrastructure.Ccxt;

/// <summary>
/// Фабрика коннекторов: создаёт экземпляры CCXT по идентификатору биржи из конфигурации.
/// Чтобы добавить новую биржу — допишите строку в карту ниже (см. README).
/// </summary>
public sealed class CcxtExchangeFactory(IEventLog log, TimeProvider time, ILoggerFactory loggerFactory) : IExchangeFactory
{
    private static readonly Dictionary<string, Func<object, Exchange>> Map = new(StringComparer.OrdinalIgnoreCase)
    {
        ["binanceusdm"] = cfg => new binanceusdm(cfg),
        ["binance"] = cfg => new binance(cfg),
        ["bybit"] = cfg => new bybit(cfg),
        ["okx"] = cfg => new okx(cfg),
        ["gate"] = cfg => new gate(cfg),
        ["mexc"] = cfg => new mexc(cfg),
        ["kucoinfutures"] = cfg => new kucoinfutures(cfg),
        ["bitget"] = cfg => new bitget(cfg),
        ["htx"] = cfg => new htx(cfg),
        ["coinex"] = cfg => new coinex(cfg),
        ["phemex"] = cfg => new phemex(cfg),
        ["krakenfutures"] = cfg => new krakenfutures(cfg),
    };

    public IReadOnlyList<string> SupportedIds => [.. Map.Keys.OrderBy(k => k, StringComparer.OrdinalIgnoreCase)];

    public IExchangeConnector Create(ExchangeConfigEntry entry, NetworkMode mode)
    {
        if (!Map.ContainsKey(entry.Id))
        {
            throw new ArgumentException(
                $"Биржа «{entry.Id}» не встроена в фабрику. Поддерживаются: {string.Join(", ", SupportedIds)}. " +
                "Как добавить новую — см. раздел «Добавление биржи» в README.");
        }

        return new CcxtExchangeConnector(entry, mode, log, time, loggerFactory.CreateLogger($"Ccxt.{entry.Id}"));
    }

    /// <summary>Создаёт экземпляр биржи CCXT (используется и пробными утилитами).</summary>
    public static Exchange Instantiate(string id, Dictionary<string, object> config) => Map.TryGetValue(id, out var make)
        ? make(config)
        : throw new ArgumentException($"Неизвестный идентификатор биржи: {id}");
}
