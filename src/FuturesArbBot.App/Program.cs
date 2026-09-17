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

// Служебные режимы проверки уведомлений: биржи не подключаются, только MAX Bot API.
if (NotificationCli.Requested(cli))
{
    var notifyCode = await NotificationCli.RunAsync(cli, cts.Token);
    WaitForKeyPress();
    return notifyCode;
}

using var app = Bootstrapper.Build(cli);
int exitCode;
try
{
    exitCode = await app.RunAsync(cts.Token);
}
catch (OperationCanceledException)
{
    Console.Error.WriteLine("Остановлено пользователем.");
    exitCode = 130;
}
catch (Exception ex)
{
    Console.Error.WriteLine($"Фатальная ошибка: {ex.Message}");
    exitCode = 1;
}

WaitForKeyPress();
return exitCode;

// Ожидание нажатия любой клавиши перед закрытием окна консоли (актуально
// при запуске двойным кликом: иначе окно закрывается мгновенно после Ctrl+C).
static void WaitForKeyPress()
{
    if (!Environment.UserInteractive || Console.IsInputRedirected)
    {
        return; // служебный/перенаправленный запуск (скрипт, редирект) — не блокируем
    }

    Console.Out.Flush();
    Console.Error.WriteLine();
    Console.Error.WriteLine("Нажмите любую клавишу, чтобы закрыть окно…");

    try
    {
        while (Console.KeyAvailable)
        {
            Console.ReadKey(intercept: true); // сбрасываем «залежавшиеся» нажатия (например, из дашборда)
        }

        Console.ReadKey(intercept: true);
    }
    catch (InvalidOperationException)
    {
        // Клавиатурный ввод через ReadKey недоступен — пробуем дождаться Enter
        try
        {
            Console.In.ReadLine();
        }
        catch
        {
            // ввод недоступен — выходим без ожидания
        }
    }
}
