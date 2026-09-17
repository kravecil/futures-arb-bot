using System.Net;
using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using FuturesArbBot.Core.Abstractions;
using FuturesArbBot.Core.Engine;
using FuturesArbBot.Infrastructure.Notifications;
using Microsoft.Extensions.Logging.Abstractions;

namespace FuturesArbBot.Tests;

/// <summary>Заготовленные ответы MAX Bot API: пишет входящие запросы и отдаёт что скажут.</summary>
internal sealed class StubHttpHandler(Func<HttpRequestMessage, HttpResponseMessage> respond) : HttpMessageHandler
{
    public List<HttpRequestMessage> Requests { get; } = [];

    public List<string> Bodies { get; } = [];

    public int CallCount => Requests.Count;

    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        Requests.Add(request);
        Bodies.Add(request.Content is null ? string.Empty : request.Content.ReadAsStringAsync(ct).GetAwaiter().GetResult());
        return Task.FromResult(respond(request));
    }
}

/// <summary>
/// HTTP-слой MAX Bot API: путь и тело запроса, заголовок авторизации без Bearer, разбор
/// ошибок (meta.code), политика повторов и long-poll /updates.
/// </summary>
public class MaxMessengerClientTests
{
    private static readonly DateTimeOffset Now = new(2026, 1, 1, 12, 0, 0, TimeSpan.Zero);

    private static BotOptions Options(Action<NotificationOptions>? tweak = null)
    {
        var options = new BotOptions();
        options.General.WriteLogFile = false;
        options.Notifications.Enabled = true;
        options.Notifications.BotToken = "top.secret.token";
        options.Notifications.AdminChatId = "42";
        options.Notifications.MaxRetries = 0;
        options.Notifications.TimeoutMs = 1_000;
        tweak?.Invoke(options.Notifications);
        return options;
    }

    private static (MaxMessengerClient Client, StubHttpHandler Handler) Create(BotOptions options, Func<HttpRequestMessage, HttpResponseMessage> respond)
    {
        var handler = new StubHttpHandler(respond);
        var config = new FakeConfigProvider(options);
        return (new MaxMessengerClient(config, new ManualTimeProvider(Now), NullLogger<MaxMessengerClient>.Instance, handler), handler);
    }

    private static HttpResponseMessage Json(HttpStatusCode status, string json) =>
        new(status) { Content = new StringContent(json, Encoding.UTF8, "application/json") };

    private static HttpResponseMessage Ok(string json = """{"message":{"mid":"1"}}""") => Json(HttpStatusCode.OK, json);

    [Fact]
    public async Task Sends_plaintext_to_messages_endpoint_with_bare_token()
    {
        var (client, handler) = Create(Options(), _ => Ok());

        var delivery = await client.SendAsync("привет");

        Assert.True(delivery.IsDelivered);
        var request = Assert.Single(handler.Requests);
        Assert.Equal(HttpMethod.Post, request.Method);
        Assert.Equal("/messages", request.RequestUri!.AbsolutePath);
        Assert.Contains("chat_id=42", request.RequestUri.Query, StringComparison.Ordinal);
        Assert.Equal("top.secret.token", request.Headers.GetValues("Authorization").Single());
        Assert.Contains("\"text\":", handler.Bodies.Single(), StringComparison.Ordinal);
        Assert.DoesNotContain("format", handler.Bodies.Single(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Text_is_serialized_as_json_string_with_unicode_escapes()
    {
        var (client, handler) = Create(Options(), _ => Ok());

        await client.SendAsync("спред +0.8%");

        using var payload = JsonDocument.Parse(handler.Bodies.Single());
        Assert.Equal("спред +0.8%", payload.RootElement.GetProperty("text").GetString());
    }

    [Fact]
    public async Task Markdown_and_silent_flags_reach_the_body()
    {
        var (client, handler) = Create(Options(n => { n.UseMarkdown = true; n.Silent = true; }), _ => Ok());

        await client.SendAsync("*жирный*");

        using var payload = JsonDocument.Parse(handler.Bodies.Single());
        Assert.Equal("markdown", payload.RootElement.GetProperty("format").GetString());
        Assert.False(payload.RootElement.GetProperty("notify").GetBoolean());
    }

    [Fact]
    public async Task Chats_message_endpoint_is_used_when_configured()
    {
        var (client, handler) = Create(Options(n => n.Endpoint = MaxSendEndpoint.ChatsMessage), _ => Ok());

        await client.SendAsync("текст");

        Assert.Equal("/chats/42/message", handler.Requests.Single().RequestUri!.AbsolutePath);
    }

    [Fact]
    public async Task Custom_base_url_is_respected()
    {
        var (client, handler) = Create(Options(n => n.ApiBaseUrl = "https://example.test/v2/"), _ => Ok());

        await client.SendAsync("текст");

        var uri = handler.Requests.Single().RequestUri!;
        Assert.Equal("example.test", uri.Host);
        Assert.Equal("/v2/messages", uri.AbsolutePath);
    }

    [Fact]
    public async Task Empty_token_or_chat_skips_http_with_permanent_error()
    {
        var (noToken, handlerA) = Create(Options(n => n.BotToken = " "), _ => Ok());
        var (noChat, handlerB) = Create(Options(n => n.AdminChatId = string.Empty), _ => Ok());

        Assert.Equal(NotificationOutcome.Permanent, (await noToken.SendAsync("текст")).Outcome);
        Assert.Equal(NotificationOutcome.Permanent, (await noChat.SendAsync("текст")).Outcome);
        Assert.Equal(0, handlerA.CallCount);
        Assert.Equal(0, handlerB.CallCount);
    }
    [Fact]
    public async Task Unauthorized_is_permanent_and_not_retried()
    {
        var (client, handler) = Create(
            Options(n => n.MaxRetries = 3),
            _ => Json(HttpStatusCode.Unauthorized, """{"meta":{"code":"verify.token","message":"Invalid access_token"}}"""));

        var delivery = await client.SendAsync("текст");

        Assert.Equal(NotificationOutcome.Permanent, delivery.Outcome);
        Assert.Contains("verify.token", delivery.Error, StringComparison.Ordinal);
        Assert.Equal(1, handler.CallCount);
    }

    [Fact]
    public async Task Rate_limit_is_retried_and_respects_retry_after()
    {
        var (client, handler) = Create(
            Options(n => n.MaxRetries = 2),
            _ =>
            {
                var response = Json(HttpStatusCode.TooManyRequests, """{"code":"common.rateLimited","message":"too many"}""");
                response.Headers.RetryAfter = new RetryConditionHeaderValue(TimeSpan.Zero);
                return response;
            });

        var delivery = await client.SendAsync("текст");

        Assert.Equal(NotificationOutcome.Transient, delivery.Outcome);
        Assert.Contains("лимит запросов", delivery.Error, StringComparison.Ordinal);
        Assert.Equal(3, handler.CallCount);
    }

    [Fact]
    public async Task Server_error_is_retried_then_reported_as_transient()
    {
        var (client, handler) = Create(Options(n => n.MaxRetries = 1), _ => Json(HttpStatusCode.BadGateway, "{}"));

        var delivery = await client.SendAsync("текст");

        Assert.False(delivery.IsDelivered);
        Assert.Equal(NotificationOutcome.Transient, delivery.Outcome);
        Assert.Equal(2, handler.CallCount);
    }

    [Fact]
    public async Task Network_failure_is_transient()
    {
        var (client, _) = Create(Options(), _ => throw new HttpRequestException("нет сети"));

        var delivery = await client.SendAsync("текст");

        Assert.Equal(NotificationOutcome.Transient, delivery.Outcome);
        Assert.Contains("нет сети", delivery.Error, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Verify_reads_bot_from_nested_or_flat_payload()
    {
        var (flat, _) = Create(Options(), _ => Ok("""{"user_id":"7","name":"Arb Bot"}"""));
        var (nested, _) = Create(Options(), _ => Ok("""{"bot":{"user_id":"7","name":"Arb Bot"},"subscriptions":[]}"""));
        var (broken, _) = Create(Options(), _ => Json(HttpStatusCode.Forbidden, """{"meta":{"code":"access.denied"}}"""));

        Assert.True((await flat.VerifyAsync()).Success);

        var info = await nested.VerifyAsync();
        Assert.True(info.Success);
        Assert.Equal("Arb Bot", info.Bot?.Name);

        var failure = await broken.VerifyAsync();
        Assert.False(failure.Success);
        Assert.Contains("access.denied", failure.Detail, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Poll_updates_collects_chat_ids_and_marker()
    {
        var payload = """
            {
              "updates": [
                { "update_type": "message_created", "message": { "recipient": { "chat_id": 42 }, "body": { "text": "привет" } } },
                { "update_type": "bot_started", "chat_id": "99" }
              ],
              "marker": 1234
            }
            """;
        var (client, handler) = Create(Options(), _ => Ok(payload));

        var page = await client.PollUpdatesAsync("7");

        Assert.Equal("1234", page.Marker);
        Assert.Equal(["42", "99"], page.Chats.Select(c => c.ChatId));
        Assert.Equal("привет", page.Chats[0].Text);
        Assert.Contains("marker=7", handler.Requests.Single().RequestUri!.Query, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Poll_updates_reports_http_error()
    {
        var (client, _) = Create(Options(), _ => Json(HttpStatusCode.Unauthorized, """{"meta":{"code":"verify.token"}}"""));

        var error = await Assert.ThrowsAsync<HttpRequestException>(() => client.PollUpdatesAsync(null));
        Assert.Contains("verify.token", error.Message, StringComparison.Ordinal);
    }
}

