using Sample.Domain.Shared.ValueObjects;

namespace Sample.Application.Features.Quotes.Services;

public static class Quote
{
    public static decimal Total(IEnumerable<Price> prices) => prices.Sum(price => price.Amount);
}
