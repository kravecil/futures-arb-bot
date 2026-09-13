namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Реестр подключённых бирж. Заполняется во время запуска приложения,
/// после чего движок и исполнитель получают коннекторы отсюда.
/// </summary>
public sealed class ConnectorRegistry : IDisposable
{
    private readonly Lock _gate = new();
    private List<IExchangeConnector> _connectors = [];

    public IReadOnlyList<IExchangeConnector> Connectors
    {
        get
        {
            lock (_gate)
            {
                return _connectors;
            }
        }
    }

    public void Replace(IEnumerable<IExchangeConnector> connectors)
    {
        List<IExchangeConnector> old;
        lock (_gate)
        {
            old = _connectors;
            _connectors = [.. connectors];
        }

        foreach (var connector in old.Where(c => !_connectors.Contains(c)))
        {
            connector.DisposeAsync().AsTask().GetAwaiter().GetResult();
        }
    }

    public void Dispose()
    {
        List<IExchangeConnector> old;
        lock (_gate)
        {
            old = _connectors;
            _connectors = [];
        }

        foreach (var connector in old)
        {
            try
            {
                connector.DisposeAsync().AsTask().GetAwaiter().GetResult();
            }
            catch
            {
                // игнорируем ошибки закрытия соединений при завершении
            }
        }
    }
}
