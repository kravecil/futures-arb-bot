using FuturesArbBot.App;
using FuturesArbBot.App.Services;

var cli = CliOptions.Parse(args);
if (cli.ShowHelp)
{
    CliOptions.PrintHelp();
    return 0;
}

using var cts = new CancellationTokenSource();
var interrupted = false;
Console.CancelKeyPress += (_, eventArgs) =>
{
    if (interrupted)
    {
        eventArgs.Cancel = false; // повторный Ctrl+C — принудительное завершение
        return;
    }

    eventArgs.Cancel = true;
    interrupted = true;
    Console.Error.WriteLine();
    Console.Error.WriteLine("^C  Останавливаюсь… повторный Ctrl+C — принудительно.");
    cts.Cancel();
};

using var app = Bootstrapper.Build(cli);
try
{
    return await app.RunAsync(cts.Token);
}
catch (OperationCanceledException)
{
    Console.Error.WriteLine("Остановлено пользователем.");
    return 130;
}
catch (Exception ex)
{
    Console.Error.WriteLine($"Фатальная ошибка: {ex.Message}");
    return 1;
}
