namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Потокобезопасный журнал событий: кольцевой буфер в памяти,
/// опциональная запись в файл logs/session-*.log.
/// </summary>
public sealed class EventLog(IConfigProvider config, TimeProvider time) : IEventLog
{
    private const int Capacity = 600;

    private readonly Lock _gate = new();
    private readonly Queue<LogEvent> _events = new(Capacity);
    private readonly Queue<LogEvent> _errors = new(120);
    private long _sequence;
    private StreamWriter? _fileSink;
    private bool _fileSinkBroken;

    public void Log(AppLogLevel level, string message)
    {
        long id;
        lock (_gate)
        {
            id = ++_sequence;
            var evt = new LogEvent(id, time.GetUtcNow(), level, message);
            _events.Enqueue(evt);
            while (_events.Count > Capacity)
            {
                _events.Dequeue();
            }

            if (level == AppLogLevel.Error)
            {
                _errors.Enqueue(evt);
                while (_errors.Count > 120)
                {
                    _errors.Dequeue();
                }
            }

            WriteToFile(evt);
        }
    }

    public IReadOnlyList<LogEvent> Latest(int count)
    {
        lock (_gate)
        {
            return [.. _events.Take(Math.Max(0, count)).OrderBy(e => e.Id)];
        }
    }

    public IReadOnlyList<LogEvent> Tail(long afterId, int max)
    {
        lock (_gate)
        {
            return [.. _events.Where(e => e.Id > afterId).Take(Math.Max(0, max))];
        }
    }

    public IReadOnlyList<LogEvent> Errors(int count)
    {
        lock (_gate)
        {
            return [.. _errors.TakeLast(Math.Max(0, count))];
        }
    }

    private void WriteToFile(LogEvent evt)
    {
        if (_fileSinkBroken || !config.Current.General.WriteLogFile)
        {
            return;
        }

        try
        {
            if (_fileSink is null)
            {
                Directory.CreateDirectory("logs");
                var path = Path.Combine("logs", $"session-{time.GetUtcNow():yyyyMMdd-HHmmss}.log");
                _fileSink = new StreamWriter(path, append: true) { AutoFlush = true };
                _fileSink.WriteLine($"# FuturesArbBot session log, открыты файлы конфигурации: {config.ConfigDirectory}");
            }

            _fileSink.WriteLine($"{evt.Time:yyyy-MM-dd HH:mm:ss.fff}\t{evt.Level}\t{evt.Message}");
        }
        catch (Exception)
        {
            // файл недоступен — молча отключаем логирование в файл, консоль продолжает работать
            _fileSinkBroken = true;
            _fileSink?.Dispose();
            _fileSink = null;
        }
    }
}

/// <summary>Сахар поверх <see cref="IEventLog"/> (C# 14: extension-члены).</summary>
public static class EventLogExtensions
{
    extension(IEventLog log)
    {
        public void Debug(string message) => log.Log(AppLogLevel.Debug, message);

        public void Info(string message) => log.Log(AppLogLevel.Info, message);

        public void Success(string message) => log.Log(AppLogLevel.Success, message);

        public void Warning(string message) => log.Log(AppLogLevel.Warning, message);

        public void Error(string message) => log.Log(AppLogLevel.Error, message);
    }
}
