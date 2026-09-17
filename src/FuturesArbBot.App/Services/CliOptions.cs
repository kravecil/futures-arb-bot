namespace FuturesArbBot.App.Services;

/// <summary>Аргументы командной строки.</summary>
public sealed record CliOptions
{
    public string? ConfigDir { get; private init; }

    /// <summary>Одно сканирование, отчёт и выход.</summary>
    public bool Once { get; private init; }

    /// <summary>Без Live-дашборда: события печатаются потоком в консоль.</summary>
    public bool NoUi { get; private init; }

    public bool ShowHelp { get; private init; }

    /// <summary>Проверить токен MAX и прислать тестовое сообщение (биржи не подключаются).</summary>
    public bool NotifyTest { get; private init; }

    /// <summary>Показать chat_id администратора: ждёт входящих сообщений от вас (биржи не подключаются).</summary>
    public bool NotifyChatId { get; private init; }

    public static CliOptions Parse(string[] args)
    {
        CliOptions options = new();
        for (var i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--once":
                    options = options with { Once = true };
                    break;

                case "--no-ui":
                    options = options with { NoUi = true };
                    break;

                case "--help":
                case "-h":
                    options = options with { ShowHelp = true };
                    return options;

                case "--config-dir":
                case "-c":
                    if (i + 1 >= args.Length)
                    {
                        throw new ArgumentException("Для --config-dir нужно указать путь.");
                    }

                    options = options with { ConfigDir = args[++i] };
                    break;

                case "--notify-test":
                    options = options with { NotifyTest = true };
                    break;

                case "--notify-chat-id":
                    options = options with { NotifyChatId = true };
                    break;

                default:
                    throw new ArgumentException($"Неизвестный аргумент: {args[i]} (см. --help)");
            }
        }

        return options;
    }

    public static void PrintHelp()
    {
        AnsiConsole.Write(new FigletText("FuturesArbBot").Color(Color.Cyan1));
        AnsiConsole.MarkupLine("[cyan]Межбиржевой арбитражный робот (фьючерсы, CCXT)[/]");
        AnsiConsole.WriteLine();
        var table = new Table().Border(TableBorder.Rounded).HideHeaders()
            .AddColumn("Аргумент")
            .AddColumn("Описание");
        table.AddRow("[cyan]--once[/]", "выполнить одно сканирование, показать сводку и выйти");
        table.AddRow("[cyan]--no-ui[/]", "без Live-дашборда; события печатаются обычным потоком");
        table.AddRow("[cyan]--config-dir <путь>[/]", "каталог конфигурации (по умолчанию: ./config)");
        table.AddRow("[cyan]--notify-test[/]", "проверить токен MAX и прислать тестовое сообщение в чат администратора");
        table.AddRow("[cyan]--notify-chat-id[/]", "показать chat_id: отправьте боту любое сообщение, робот прочитает его из /updates");
        table.AddRow("[cyan]--help[/]", "показать справку");
        AnsiConsole.Write(table);
        AnsiConsole.MarkupLine("[grey]Примеры:[/] dotnet run --project src/FuturesArbBot.App -- --once");
        AnsiConsole.MarkupLine("       dotnet run --project src/FuturesArbBot.App -- --config-dir ./config");
        AnsiConsole.MarkupLine("       dotnet run --project src/FuturesArbBot.App -- --notify-chat-id");
        AnsiConsole.WriteLine();
        AnsiConsole.MarkupLine("[grey]Уведомления настраиваются в config/notifications.json (раздел Notifications); токен безопаснее задавать переменной ARB_Notifications__BotToken.[/]");
        AnsiConsole.WriteLine();
        AnsiConsole.MarkupLine("[yellow]Ctrl+C[/] — корректная остановка с подробной статистикой сеанса.");
    }
}
