import { add, multiply } from "./math";

export function calculateTotal(price: number, count: number): number {
  const subtotal = multiply(price, count);
  return add(subtotal, 10);
}

export class InvoiceService {
  total(price: number, count: number): number {
    return calculateTotal(price, count);
  }
}
