import type { InputDelivery } from "@/lib/assistant/contract";

const STORAGE_KEY = "cosir:input-delivery";
const DEFAULT_DELIVERY: InputDelivery = "immediate";

/** 读取运行中发送消息的投递方式偏好；未保存或存储不可用时返回默认的立即并入。 */
export function readStoredInputDelivery(): InputDelivery {
  if (typeof window === "undefined") return DEFAULT_DELIVERY;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw === "immediate" || raw === "next_run" ? raw : DEFAULT_DELIVERY;
  } catch {
    return DEFAULT_DELIVERY;
  }
}

/** 写入投递方式偏好；只影响下一次发送的请求字段，不承载 inbox 事实。 */
export function writeStoredInputDelivery(delivery: InputDelivery): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, delivery);
  } catch {
    return;
  }
}
