using System.Collections.Concurrent;
using System.Text.RegularExpressions;

namespace FuturesArbBot.Core.Engine;

/// <summary>
/// Фильтр торговых пар: только линейные бессрочные фьючерсы в заданных котирах,
/// с достаточным оборотом, прошедшие белый и чёрный списки (wildcard-шаблоны).
/// </summary>
public sealed partial class SymbolFilter(IConfigProvider config) : ISymbolFilter
{
    private static readonly ConcurrentDictionary<string, Regex> RegexCache = new(StringComparer.Ordinal);

    private readonly SymbolsOptions _options = config.Current.Symbols;

    public bool IsAllowed(MarketInfo market, decimal quoteVolume24h)
    {
        if (!market.IsPerpetual || !market.IsLinear)
        {
            return false;
        }

        if (_options.QuoteCurrencies.Count > 0
            && !_options.QuoteCurrencies.Contains(market.QuoteCurrency, StringComparer.OrdinalIgnoreCase))
        {
            return false;
        }

        if (quoteVolume24h > 0m && quoteVolume24h < _options.MinQuoteVolume24hUsd)
        {
            return false;
        }

        if (_options.Include.Count > 0 && !_options.Include.Any(pattern => Wildcard(pattern).IsMatch(market.Symbol)))
        {
            return false;
        }

        return !_options.Exclude.Any(pattern => Wildcard(pattern).IsMatch(market.Symbol));
    }

    /// <summary>Превращает шаблон с * и ? в скомпилированный кешированный regex.</summary>
    private static Regex Wildcard(string pattern) => RegexCache.GetOrAdd(pattern, static p =>
    {
        var source = Regex.Escape(p).Replace(@"\*", ".*", StringComparison.Ordinal).Replace(@"\?", ".", StringComparison.Ordinal);
        return new Regex($"^{source}$", RegexOptions.IgnoreCase | RegexOptions.Compiled | RegexOptions.CultureInvariant);
    });
}
