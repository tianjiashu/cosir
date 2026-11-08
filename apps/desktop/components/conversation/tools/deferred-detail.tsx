import { memo, useState, type ReactNode } from "react";

const DetailBody = memo(function DetailBody({ render }: { render: () => ReactNode }) {
  return render();
});

/** 首次可见才构建详情；看过后保留 DOM、选区及内部滚动，隐藏时冻结展示输入。独立弹层不放在这个边界里。 */
export function DeferredDetail({ visible, render }: { visible: boolean; render: () => ReactNode }) {
  const [shown, setShown] = useState<(() => ReactNode) | null>(() => visible ? render : null);
  if (visible && shown !== render) setShown(() => render);
  return shown && <DetailBody render={shown} />;
}
