export type Price = { readonly amount: number; readonly currency: string };

export const total = (prices: readonly Price[]): number => prices.reduce((sum, price) => sum + price.amount, 0);
