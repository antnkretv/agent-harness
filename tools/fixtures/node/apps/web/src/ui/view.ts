import { quote } from "../application/quote";

export const render = (): string => quote([{ amount: 2, currency: "EUR" }]);
