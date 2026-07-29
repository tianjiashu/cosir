import { calculateTotal, InvoiceService } from "./service";

test("calculateTotal adds fixed fee", () => {
  expect(calculateTotal(5, 2)).toBe(20);
});

test("InvoiceService delegates total", () => {
  const service = new InvoiceService();
  expect(service.total(5, 2)).toBe(20);
});
