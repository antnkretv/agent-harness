using Sample.Application.Features.Quotes.Services;
using Sample.Domain.Shared.ValueObjects;

namespace Sample.Infrastructure.Features.Quotes.Repositories;

public sealed class QuoteStore
{
    private readonly List<Price> _prices = [];

    public void Add(Price price) => _prices.Add(price);

    public decimal Total() => Quote.Total(_prices);
}
