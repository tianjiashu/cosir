import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/logging/frontend-log", () => ({ frontendLog: vi.fn() }));

import { BrowserDialogBanner } from "@/components/browser/dialog-banner";
import { DownloadBar } from "@/components/browser/download-bar";
import type { BrowserDownload, CosirHost } from "@/electron/shared/host-contract";

const host = { browser: {} } as unknown as CosirHost;

function download(id: string, patch: Partial<BrowserDownload> = {}): BrowserDownload {
  return { id, taskId: 7, tabId: "t1", filename: `${id}.zip`, path: `/ws/downloads/${id}.zip`, state: "completed", stalled: false, receivedBytes: 10, totalBytes: 10, ...patch };
}

describe("BrowserDialogBanner", () => {
  it("alert 只有确定，confirm 有确定与取消，prompt 预填默认值", () => {
    const alert = renderToStaticMarkup(<BrowserDialogBanner host={host} tabId="t1" dialog={{ id: 1, type: "alert", message: "保存成功", defaultText: "" }} />);
    expect(alert).toContain("保存成功");
    expect(alert).toContain("确定");
    expect(alert).not.toContain("取消");

    const confirm = renderToStaticMarkup(<BrowserDialogBanner host={host} tabId="t1" dialog={{ id: 2, type: "confirm", message: "确定删除？", defaultText: "" }} />);
    expect(confirm).toContain("取消");
    expect(confirm).not.toContain("弹窗输入");

    const prompt = renderToStaticMarkup(<BrowserDialogBanner host={host} tabId="t1" dialog={{ id: 3, type: "prompt", message: "你的名字", defaultText: "张三" }} />);
    expect(prompt).toContain('aria-label="弹窗输入"');
    expect(prompt).toContain('value="张三"');
  });
});

describe("DownloadBar", () => {
  it("平时只显示最新一次下载，多于一次时给出展开入口", () => {
    const html = renderToStaticMarkup(<DownloadBar host={host} downloads={[download("a"), download("b")]} />);
    expect(html).toContain("b.zip");
    expect(html).not.toContain("a.zip");
    expect(html).toContain("全部 2 个");
  });

  it("进行中显示进度，总量未知只显示已收；完成的才能在文件夹中显示；失败显示结局", () => {
    const progressing = renderToStaticMarkup(
      <DownloadBar host={host} downloads={[download("a", { state: "progressing", receivedBytes: 512 * 1024, totalBytes: 2 * 1024 * 1024 })]} />,
    );
    expect(progressing).toContain("512.0 KB / 2.0 MB · 25%");
    expect(progressing).not.toContain("在文件夹中显示");

    const unknownTotal = renderToStaticMarkup(<DownloadBar host={host} downloads={[download("a", { state: "progressing", receivedBytes: 2048, totalBytes: 0 })]} />);
    expect(unknownTotal).toContain("2.0 KB");
    expect(unknownTotal).not.toContain("%");

    expect(renderToStaticMarkup(<DownloadBar host={host} downloads={[download("a")]} />)).toContain("在文件夹中显示 a.zip");
    expect(renderToStaticMarkup(<DownloadBar host={host} downloads={[download("a", { state: "interrupted" })]} />)).toContain("下载失败");
    // 断线续传不是失败：先报「下载失败」再变回进度条会让用户以为要重下。
    const stalled = renderToStaticMarkup(<DownloadBar host={host} downloads={[download("a", { state: "progressing", stalled: true })]} />);
    expect(stalled).toContain("连接中断，正在重试");
    expect(stalled).not.toContain("下载失败");
  });
});
