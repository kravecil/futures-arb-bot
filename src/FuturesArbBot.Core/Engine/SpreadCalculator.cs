namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Расчёт спреда между двумя биржами. Брутто = (bid богатых − ask дешёвой) / ask дешёвой.
/// Нетто = брутто − комиссии taker полного круга − буфер на проскальзывание. Полного круга,
/// потому что пару не только открывают двумя ногами (buy дешёвой + sell богатых), но и закрывают
/// двумя (sell дешёвой + buy богатых): четыре комиссии taker. Считать только вход — значит вдвое
/// занижать стоимость сделки и пропускать сигналы, убыточные уже на пороге входа.
/// </summary>
public sealed class SpreadCalculator(IConfigProvider config, TimeProvider time) : ISpreadCalculator
{
    /// <summary>Открытие и закрытие пары — два прохода по обеим ногам.</summary>
    private const decimal RoundTripMultiplier = 2m;

    public SpreadEstimate? Calculate(TickerSnapshot cheaper, TickerSnapshot richer, ExchangeFees cheapFees, ExchangeFees richFees)
    {
        if (cheaper.ExchangeId == richer.ExchangeId || !cheaper.IsTradable || !richer.IsTradable)
        {
            return null;
        }

        if (richer.Bid <= cheaper.Ask)
        {
            return null; // перекрытия нет — возможности нет
        }

        var options = config.Current.Arbitrage;
        var gross = (richer.Bid - cheaper.Ask) / cheaper.Ask * 100m;

        if (gross > options.MaxSpreadPercent)
        {
            // «слишком хорошо, чтобы быть правдой»: новые листинги/разные индексы — не арбитраж
            return null;
        }

        var entryFees = options.IncludeFees ? cheapFees.TakerPercent + richFees.TakerPercent : 0m;
        var feeCost = entryFees * RoundTripMultiplier;
        var net = gross - feeCost - options.SlippageBufferPercent;

        if (net <= 0m)
        {
            return null;
        }

        return new SpreadEstimate(
            cheaper.Symbol,
            new OpportunityLeg(cheaper.ExchangeId, cheaper.Ask, cheapFees.TakerPercent),
            new OpportunityLeg(richer.ExchangeId, richer.Bid, richFees.TakerPercent),
            Math.Round(gross, 4),
            Math.Round(net, 4),
            Math.Min(cheaper.QuoteVolume24h, richer.QuoteVolume24h),
            time.GetUtcNow());
    }
}
