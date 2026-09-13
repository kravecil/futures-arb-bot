namespace FuturesArbBot.App.Services;

/// <summary>
/// Текстовый режим (--no-ui): печатает новые события журнала в консоль,
/// без Live-рендеринга (удобно для отладки и терминалов без ANSI).
/// </summary>
public sealed class SimpleConsoleFeed(IEventLog log)
{
    public async Task RunAsync(CancellationToken ct)
    {
        long after = 0;
        while (!ct.IsCancellationRequested)
        {
            var events = log.Tail(after, 50);
            foreach (var evt in events)
            {
                after = evt.Id;
                if (evt.Level == AppLogLevel.Debug)
                {
                    continue;
                }

                AnsiConsole.MarkupLineInterpolated(
                    $"[dim]{evt.Time.ToLocalTime():HH:mm:ss}[/] [{evt.Level.ColorMarkup}]{Markup.Escape(evt.Message)}[/]");
            }

            try
            {
                await Task.Delay(250, ct);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }
}
