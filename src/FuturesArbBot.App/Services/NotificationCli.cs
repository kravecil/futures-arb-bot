namespace FuturesArbBot.App.Services;

/// <summary>
/// Служебные режимы настройки уведомлений: <c>--notify-test</c> и <c>--notify-chat-id</c>.
/// Работают без подключения бирж — только MAX Bot API и config/notifications.json.
/// </summary>
public static class NotificationCli
{
    /// <summary>Сколько секунд слушаем <c>/updates</c> в режиме поиска chat_id.</summary>
    private const int WatchSeconds = 90;

    /// <summary>Запрошен ли один из служебных режимов.</summary>
    public static bool Requested(CliOptions cli) => cli.NotifyTest || cli.NotifyChatId;

    /// <summary>Проверить токен, а дальше — тестовая отправка либо поиск chat_id. Возвращает код выхода.</summary>
    public static async Task<int> RunAsync(CliOptions cli, CancellationToken ct)
    {
        using var provider = Bootstrapper.BuildProvider(cli);
        var config = provider.GetRequiredService<IConfigProvider>();
        var client = provider.GetRequiredService<MaxMessengerClient>();
        var notifications = config.Current.Notifications;

        PrintState(notifications);

        AnsiConsole.MarkupLineInterpolated($"[cyan]→[/] проверяю токен: GET {Markup.Escape(notifications.ApiBaseUrl)}/me");
        var verify = await client.VerifyAsync(ct);
        if (!verify.Success)
        {
            AnsiConsole.MarkupLineInterpolated($"[red]✗ токен не принят:[/] {Markup.Escape(verify.Detail)}");
            AnsiConsole.MarkupLine("[grey]Создайте бота в диалоге с @MasterBot и запишите токен в Notifications:BotToken или в ARB_Notifications__BotToken.[/]");
            return 2;
        }

        AnsiConsole.MarkupLineInterpolated($"[green]✓[/] {Markup.Escape(verify.Detail)}");

        return cli.NotifyChatId
            ? await WatchChatIdAsync(client, ct)
            : await SendTestAsync(client, config, ct);
    }

    /// <summary>Отправить проверочное сообщение в чат из конфигурации.</summary>
    private static async Task<int> SendTestAsync(MaxMessengerClient client, IConfigProvider config, CancellationToken ct)
    {
        var notifications = config.Current.Notifications;
        if (string.IsNullOrWhiteSpace(notifications.AdminChatId))
        {
            AnsiConsole.MarkupLine("[red]✗ Notifications:AdminChatId пуст.[/] Сначала выполните: --notify-chat-id");
            return 2;
        }

        if (!notifications.Enabled)
        {
            AnsiConsole.MarkupLine("[yellow]![/] Notifications:Enabled = false — сообщение отправляю вручную, сигналы робота при этом не шлются.");
        }

        var text = NotificationFormatter.TestMessage(DateTimeOffset.UtcNow, config.Current.General.NetworkMode);
        AnsiConsole.MarkupLineInterpolated($"[cyan]→[/] отправляю тестовое сообщение в чат {Markup.Escape(notifications.AdminChatId)}…");

        var delivery = await client.SendAsync(text, ct);
        if (delivery.IsDelivered)
        {
            AnsiConsole.MarkupLine("[green]✓ сообщение доставлено.[/] Задайте Notifications:Enabled = true — и сигналы начнут приходить.");
            return 0;
        }

        AnsiConsole.MarkupLineInterpolated($"[red]✗ доставка не удалась:[/] {Markup.Escape(delivery.Error ?? "неизвестная ошибка")}");
        AnsiConsole.MarkupLine(notifications.UseMarkdown
            ? "[grey]400 — возможно, чат не принимает разметку: отключите UseMarkdown. 401/403 — токен, 404 — AdminChatId.[/]"
            : "[grey]401/403 — проверьте BotToken, 404 — AdminChatId (и что вы написали боту первым).[/]");
        return 2;
    }

    /// <summary>Слушать входящие апдейты и показывать chat_id диалогов, где боту уже написали.</summary>
    private static async Task<int> WatchChatIdAsync(MaxMessengerClient client, CancellationToken ct)
    {
        AnsiConsole.MarkupLine("[yellow]Отправьте боту любое сообщение в Max[/] — прочитаю chat_id этого диалога. Ctrl+C — выход.");

        var deadline = DateTimeOffset.UtcNow.AddSeconds(WatchSeconds);
        var known = new HashSet<string>(StringComparer.Ordinal);
        string? marker = null;

        while (DateTimeOffset.UtcNow < deadline && !ct.IsCancellationRequested)
        {
            MaxUpdatesPage page;
            try
            {
                page = await client.PollUpdatesAsync(marker, ct);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception ex)
            {
                AnsiConsole.MarkupLineInterpolated($"[red]✗ не удалось прочитать /updates:[/] {Markup.Escape(ex.Message)}");
                AnsiConsole.MarkupLine("[grey]Бот не может начать диалог сам: убедитесь, что токен ваш и вы уже написали боту.[/]");
                return 2;
            }

            marker = page.Marker ?? marker;
            foreach (var hit in page.Chats)
            {
                if (!known.Add(hit.ChatId))
                {
                    continue;
                }

                var note = string.IsNullOrWhiteSpace(hit.Text) ? hit.UpdateType : $"{hit.UpdateType}: {Trim(hit.Text)}";
                AnsiConsole.MarkupLineInterpolated($"[green]✓ chat_id[/] {Markup.Escape(hit.ChatId)} [grey]{Markup.Escape(note ?? "update")}[/]");
            }
        }

        if (known.Count == 0)
        {
            AnsiConsole.MarkupLine("[yellow]Сообщений не увидено.[/] Напишите боту и повторите: --notify-chat-id");
            return 1;
        }

        AnsiConsole.MarkupLine("[grey]Скопируйте нужный chat_id в Notifications:AdminChatId и включите Enabled = true.[/]");
        return 0;
    }

    private static string Trim(string text) => text.Length <= 60 ? text : text[..60] + "…";

    /// <summary>Показать, что сейчас настроено в разделе Notifications (без раскрытия токена).</summary>
    private static void PrintState(NotificationOptions notifications)
    {
        var table = new Table().Border(TableBorder.Rounded).HideHeaders()
            .AddColumn("Параметр")
            .AddColumn("Значение");

        table.AddRow("Enabled", Describe(notifications.Enabled));
        table.AddRow("BotToken", Mask(notifications.BotToken));
        table.AddRow("AdminChatId", string.IsNullOrWhiteSpace(notifications.AdminChatId) ? "[red]не задан[/]" : Markup.Escape(notifications.AdminChatId));
        table.AddRow("ApiBaseUrl", Markup.Escape(notifications.ApiBaseUrl));
        table.AddRow("Endpoint", notifications.Endpoint.ToString());
        table.AddRow("MinSpreadPercent", notifications.MinSpreadPercent is { } min ? Formatting.Pct(min) : "наследует Arbitrage:MinSpreadPercentUp");
        table.AddRow("UseMarkdown / Silent", $"{Describe(notifications.UseMarkdown)} / {Describe(notifications.Silent)}");
        table.AddRow("Cooldown / MaxPerMinute", $"{notifications.SignalCooldownSeconds} c / {notifications.MaxPerMinute} сообщ.");
        table.AddRow("Retries / Circuit", $"{notifications.MaxRetries} / {notifications.FailureCircuitLimit}→{notifications.CircuitBreakMinutes} мин");

        AnsiConsole.MarkupLine("[cyan]Уведомления (config/notifications.json → Notifications)[/]");
        AnsiConsole.Write(table);
        AnsiConsole.WriteLine();
    }

    private static string Describe(bool value) => value ? "[green]да[/]" : "[grey]нет[/]";

    private static string Mask(string token) => string.IsNullOrWhiteSpace(token)
        ? "[red]не задан[/]"
        : token.Length <= 8
            ? "[yellow]задан (короткий)[/]"
            : Markup.Escape($"{token[..4]}…{token[^4..]} ({token.Length} симв.)");
}
