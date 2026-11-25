import { useEffect, useState } from "react";

/**
 * `active` 为真时立即为真；变假后再保持 `ms` 毫秒才变假，期间又变真则继续保持。
 *
 * 给「一连串短暂真值」用：例如 Agent 逐个调用浏览器工具时，宿主的「此刻在执行」在两次调用之间会归零，直接拿来
 * 驱动提醒会一闪一灭。只平滑显示，不改事实本身。
 *
 * `subject` 是这串真值属于谁（如会话 id）：保持期只属于它，换了主体立即作废。调用方常挂在不随主体重建的
 * 常驻组件里（页头），不按主体区分会把上一个会话的「刚才在用」带到下一个会话。
 */
export function useLingering(active: boolean, ms: number, subject: unknown): boolean {
  const [lingering, setLingering] = useState<{ subject: unknown } | null>(active ? { subject } : null);

  useEffect(() => {
    if (active) {
      setLingering({ subject });
      return;
    }
    const timer = setTimeout(() => setLingering(null), ms);
    return () => clearTimeout(timer);
  }, [active, ms, subject]);

  return active || (lingering !== null && Object.is(lingering.subject, subject));
}
