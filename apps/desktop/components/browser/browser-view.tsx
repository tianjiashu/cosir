import { ArrowLeftIcon, ArrowRightIcon, RotateCwIcon } from "lucide-react";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { TooltipIconButton } from "@/components/ui/tooltip-icon-button";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Spinner } from "@/components/ui/spinner";
import { EmptyState } from "@/components/ui/state-panel";
import type { BrowserNavigation, CosirHost } from "@/electron/shared/host-contract";
import { watchOverlays } from "@/lib/browser/overlay-watch";
import { displayedAddress } from "@/lib/browser/tab-label";
import { frontendLog } from "@/lib/logging/frontend-log";
import { notify } from "@/lib/notify";

/**
 * 一个标签页的实时画面：地址栏 + 一块占位区域，网页由宿主作为原生画面放在占位区域上。
 *
 * 占位区域只上报「想放在哪」，摆放事实在宿主（同一时刻只挂一个标签）。组件卸载（切标签、收起面板、
 * 离开对话页）即收起画面，页面在后台继续工作，Agent 不受影响。弹层盖到占位区域时先放冻结帧再收起画面，
 * 弹层消失后恢复实时画面（设计稿 §6.1）。
 *
 * 视口归宿主（`TabEmulation`）：默认跟随占位区域的大小，网页按真实大小铺满；面板尺寸停稳之前，或 Agent 固定了
 * 视口时，宿主把视口画面等比缩放、水平居中、顶部对齐放进占位矩形，宽高比不同时留白露出这里的底色。冻结帧是
 * 视口的截图，所以 `<img>` 用 `object-contain object-top`——与宿主同一摆放规则，换上冻结帧时画面不跳；宿主的
 * 摆放规则变了，这里要一起改。
 */
export function BrowserView({ host, tabId, url, loading, crashed }: {
  host: CosirHost;
  tabId: string;
  url: string;
  loading: boolean;
  crashed: boolean;
}) {
  const slot = useRef<HTMLDivElement>(null);
  const [frozen, setFrozen] = useState<string | null>(null);
  const [address, setAddress] = useState(url);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    if (!editing) setAddress(displayedAddress(url));
  }, [url, editing]);

  useEffect(() => {
    // 崩溃的页面不放上来：原生画面会盖住下面的「页面已崩溃」提示。
    if (crashed) return;
    const element = slot.current!;
    let covered = false;
    let disposed = false;
    // 每次遮挡状态变化递增；冻结帧是异步取的，回来时状态已变就丢弃。
    let generation = 0;
    const place = (visible: boolean) => {
      const rect = element.getBoundingClientRect();
      const placement = visible ? { x: rect.left, y: rect.top, width: rect.width, height: rect.height } : null;
      host.browser.place(tabId, placement).catch((error: unknown) => {
        void frontendLog("WARNING", "browser_place_failed", "内置浏览器画面摆放失败", { data: { tabId }, error });
      });
    };
    const report = () => {
      if (!covered && !disposed) place(true);
    };
    const resizeObserver = new ResizeObserver(report);
    resizeObserver.observe(element);
    // 浏览器面板贴在窗口右侧：窗口变宽变窄时位置会变而尺寸不变，ResizeObserver 收不到。
    window.addEventListener("resize", report);
    const stopWatching = watchOverlays(element, (isCovered) => {
      covered = isCovered;
      const current = ++generation;
      if (!isCovered) {
        setFrozen(null);
        report();
        return;
      }
      host.browser.freeze(tabId).then(
        (image) => {
          if (disposed || current !== generation) return;
          setFrozen(image);
          // 先让冻结帧画出来，再收起原生画面，中间不闪白。
          requestAnimationFrame(() => {
            if (!disposed && current === generation) place(false);
          });
        },
        () => {
          if (!disposed && current === generation) place(false);
        },
      );
    });
    report();
    return () => {
      disposed = true;
      resizeObserver.disconnect();
      window.removeEventListener("resize", report);
      stopWatching();
      place(false);
    };
  }, [host, tabId, crashed]);

  const navigate = (navigation: BrowserNavigation) => {
    host.browser.navigate(tabId, navigation).catch((error: unknown) => {
      notify.error("页面打开失败", { error, fallback: "请检查地址后重试" });
    });
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const target = address.trim();
    if (!target) return;
    setEditing(false);
    navigate({ url: target });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      <form className="flex shrink-0 items-center gap-1 border-b px-2 py-1.5" onSubmit={submit}>
        <TooltipIconButton tooltip="后退" type="button" className="size-7" onClick={() => navigate({ action: "back" })}>
          <ArrowLeftIcon />
        </TooltipIconButton>
        <TooltipIconButton tooltip="前进" type="button" className="size-7" onClick={() => navigate({ action: "forward" })}>
          <ArrowRightIcon />
        </TooltipIconButton>
        <TooltipIconButton tooltip="刷新" type="button" className="size-7" onClick={() => navigate({ action: "reload" })}>
          {loading ? <Spinner className="size-3.5" /> : <RotateCwIcon />}
        </TooltipIconButton>
        <Input
          aria-label="网址"
          className="h-7 flex-1 text-xs"
          value={address}
          spellCheck={false}
          onFocus={() => setEditing(true)}
          onBlur={() => setEditing(false)}
          onChange={(event) => setAddress(event.target.value)}
        />
      </form>
      <div ref={slot} className="bg-muted/30 relative min-h-0 flex-1" data-browser-slot={tabId}>
        {frozen && <img src={frozen} alt="" className="absolute inset-0 size-full object-contain object-top" />}
        {crashed && (
          <div className="absolute inset-0 flex items-center justify-center">
            <EmptyState
              title="页面已崩溃"
              action={<Button type="button" variant="outline" size="sm" onClick={() => navigate({ action: "reload" })}>刷新</Button>}
            />
          </div>
        )}
      </div>
    </div>
  );
}
