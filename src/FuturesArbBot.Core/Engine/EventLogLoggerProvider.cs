namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Перенаправляет записи Microsoft.Extensions.Logging в журнал событий,
/// чтобы они попадали и в дашборд, и в файловый лог.
/// </summary>
public sealed class EventLogLoggerProvider(IEventLog sink) : ILoggerProvider
{
    public ILogger CreateLogger(string categoryName) => new EventLogLogger(sink, Shorten(categoryName));

    public void Dispose()
    {
    }

    private static string Shorten(string category)
    {
        var dot = category.LastIndexOf('.');
        return dot > 0 ? category[(dot + 1)..] : category;
    }

    private sealed class EventLogLogger(IEventLog sink, string category) : ILogger
    {
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

        public bool IsEnabled(LogLevel logLevel) => logLevel >= LogLevel.Information;

        public void Log<TState>(LogLevel logLevel, EventId eventId, TState state, Exception? exception, Func<TState, Exception?, string> formatter)
        {
            if (!IsEnabled(logLevel))
            {
                return;
            }

            var message = formatter(state, exception);
            if (exception is not null)
            {
                message += $" :: {exception.GetType().Name}: {exception.Message}";
            }

            var level = logLevel switch
            {
                LogLevel.Error or LogLevel.Critical => AppLogLevel.Error,
                LogLevel.Warning => AppLogLevel.Warning,
                _ => AppLogLevel.Info,
            };

            sink.Log(level, $"[{category}] {message}");
        }
    }
}
