import { type Price, total } from "../domain/price";

export const quote = (prices: readonly Price[]): string => `${total(prices)}`;
