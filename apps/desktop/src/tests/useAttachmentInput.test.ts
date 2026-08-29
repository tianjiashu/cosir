// @vitest-environment happy-dom
/**
 * useAttachmentInput hook 纯逻辑测试（附件状态机 + 发送前校验 + 类型推断/合并）。
 *
 * 覆盖目标（与后端 CreateTaskRequest/CreateTurnRequest 契约对齐）：
 * - 附件增删/清空/失败恢复（setRestoredAttachments）；
 * - validateForSend 各分支：数量上限、ref 非空、ref 超长、url 协议、模型视觉能力拦截；
 * - 纯函数 inferAttachmentKind / pathToAttachment / urlToAttachment / directoryToAttachment / mergeAttachments 的边界。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import {
  ATTACHMENT_MAX_COUNT,
  ATTACHMENT_MAX_REF_LEN,
  directoryToAttachment,
  inferAttachmentKind,
  mergeAttachments,
  pathToAttachment,
  urlToAttachment,
  useAttachmentInput,
} from "@/components/layout/useAttachmentInput";
import type { AttachmentRef } from "@shared/attachment";

vi.mock("@/lib/logger", () => ({
  logInfo: () => {},
  logWarn: () => {},
  logError: () => {},
}));

describe("inferAttachmentKind 类型推断", () => {
  // 测试目的：http(s) 开头应推断为 url 类型；潜在缺陷：大小写/空白处理不一致。
  it("http/https 开头推断为 url（含首尾空白与大小写）", () => {
    expect(inferAttachmentKind("https://example.com/x")).toBe("url");
    expect(inferAttachmentKind("HTTP://example.com")).toBe("url");
    expect(inferAttachmentKind("  https://a.b/c  ")).toBe("url");
  });

  // 测试目的：图片扩展名应推断 image；潜在缺陷：非常见扩展名或大写漏判。
  it("图片扩展名推断为 image（含大写/无扩展名目录）", () => {
    expect(inferAttachmentKind("C:\\tmp\\a.PNG")).toBe("image");
    expect(inferAttachmentKind("/home/u/photo.jpeg")).toBe("image");
    expect(inferAttachmentKind("C:\\tmp\\a.webp")).toBe("image");
  });

  // 测试目的：非图片非 url 的本地路径推断为 file；潜在缺陷：无扩展名被误判。
  it("普通文件路径推断为 file，无扩展名按 file 处理", () => {
    expect(inferAttachmentKind("/etc/hosts")).toBe("file");
    expect(inferAttachmentKind("C:\\tmp\\README")).toBe("file");
  });
});

describe("AttachmentRef 构造纯函数", () => {
  // 测试目的：pathToAttachment 经推断设置 kind；urlToAttachment trim 空白；directoryToAttachment 恒为 directory。
  it("pathToAttachment 按扩展名设 kind", () => {
    expect(pathToAttachment("a.png")).toEqual({ kind: "image", ref: "a.png" });
  });
  it("urlToAttachment 保留原始 ref 但内部 trim（validateForSend 用 trim 后结果）", () => {
    expect(urlToAttachment("  https://x.com  ")).toEqual({ kind: "url", ref: "https://x.com" });
  });
  it("directoryToAttachment 恒为 directory 类型", () => {
    expect(directoryToAttachment("/data/project")).toEqual({ kind: "directory", ref: "/data/project" });
  });
});

describe("mergeAttachments 去重保持顺序", () => {
  // 测试目的：后选不覆盖先选、按 ref 去重；潜在缺陷：同 ref 不同 kind 被错误合并。
  it("重复 ref 去重且保留先出现项顺序", () => {
    const current: AttachmentRef[] = [{ kind: "file", ref: "a.txt" }];
    const incoming: AttachmentRef[] = [
      { kind: "file", ref: "a.txt" },
      { kind: "image", ref: "b.png" },
    ];
    const merged = mergeAttachments(current, incoming);
    expect(merged).toEqual([
      { kind: "file", ref: "a.txt" },
      { kind: "image", ref: "b.png" },
    ]);
  });
});

describe("useAttachmentInput 增删/清空/恢复", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  // 测试目的：addLocalFilePaths 按扩展名推断并合并；潜在缺陷：空数组也触发 setState。
  it("addLocalFilePaths 推断 image/file 并合并", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(["/x/screen.png", "/y/notes.md"]);
    });
    expect(result.current.attachments).toEqual([
      { kind: "image", ref: "/x/screen.png" },
      { kind: "file", ref: "/y/notes.md" },
    ]);
  });

  // 测试目的：addDirectoryPath 生成 directory 类型且空串被忽略。
  it("addDirectoryPath 仅目录类型，空串忽略", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addDirectoryPath("");
      result.current.addDirectoryPath("/data/proj");
    });
    expect(result.current.attachments).toEqual([{ kind: "directory", ref: "/data/proj" }]);
  });

  // 测试目的：addUrl 生成 url 类型且空串忽略；潜在缺陷：未 trim 导致空 url 进入校验。
  it("addUrl 生成 url 类型且空串忽略", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addUrl("   ");
      result.current.addUrl("https://example.com");
    });
    expect(result.current.attachments).toEqual([{ kind: "url", ref: "https://example.com" }]);
  });

  // 测试目的：remove 按 ref 移除单个附件，数组减少且类型正确；潜在缺陷：ref 不相等时误删。
  it("remove 按 ref 移除单个附件", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(["/a.png"]);
      result.current.addDirectoryPath("/dir");
    });
    expect(result.current.attachments).toHaveLength(2);
    act(() => {
      result.current.removeAttachment("/a.png");
    });
    expect(result.current.attachments).toEqual([{ kind: "directory", ref: "/dir" }]);
  });

  // 测试目的：clear 清空全部；setRestoredAttachments 用快照恢复（失败回滚链路）。
  it("clear 清空 + setRestoredAttachments 恢复快照", () => {
    const snapshot: AttachmentRef[] = [{ kind: "file", ref: "keep.md" }];
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(["/a.png", "/b.md"]);
    });
    act(() => {
      result.current.clear();
    });
    expect(result.current.attachments).toHaveLength(0);
    act(() => {
      result.current.setRestoredAttachments(snapshot);
    });
    expect(result.current.attachments).toEqual(snapshot);
  });

  // 测试目的：hasImage 是否准确反映是否含 image 类型；潜在缺陷：directory 误判为 image。
  it("hasImage 仅当存在 image 类型为 true", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addDirectoryPath("/dir");
      result.current.addUrl("https://x.com");
    });
    expect(result.current.hasImage()).toBe(false);
    act(() => {
      result.current.addLocalFilePaths(["/shot.png"]);
    });
    expect(result.current.hasImage()).toBe(true);
  });
});

describe("validateForSend 规则 1：数量上限", () => {
  beforeEach(() => vi.clearAllMocks());

  // 测试目的：>20 个附件拦截；潜在缺陷：边界 20 被误判为超限。
  it("附件数 = 21 → ok:false 且提示数量超限", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(Array.from({ length: 21 }, (_, i) => `/f${i}.txt`));
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(undefined);
    });
    expect(validation.ok).toBe(false);
    expect(validation.message).toContain(String(ATTACHMENT_MAX_COUNT));
  });

  // 测试目的：=20 通过；潜在缺陷：off-by-one 把 20 判为超限。
  it("附件数 = 20 → ok:true（边界通过）", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(Array.from({ length: 20 }, (_, i) => `/f${i}.txt`));
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(true);
    });
    expect(validation.ok).toBe(true);
  });
});

describe("validateForSend 规则 2：ref 非空 / ref 超长 / url 协议", () => {
  beforeEach(() => vi.clearAllMocks());

  // 测试目的：ref 去空白后为空拦截；潜在缺陷：仅 trim 前判空导致空白 ref 漏过。
  it("ref 仅空白 → ok:false 提示不能为空", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addUrl("   ");
    });
    // addUrl 已忽略空白，需走 path/url 直接构造难以触发；这里用 directory 路径空白不可达，
    // 改为通过 addLocalFilePaths 传入空白路径（推断为 file，ref 保留空白）。
    const { result: r2 } = renderHook(() => useAttachmentInput());
    act(() => {
      r2.current.addLocalFilePaths(["   "]);
    });
    let validation!: ReturnType<typeof r2.current.validateForSend>;
    act(() => {
      validation = r2.current.validateForSend(true);
    });
    expect(validation.ok).toBe(false);
    expect(validation.message).toContain("不能为空");
  });

  // 测试目的：ref 长度 > 4096 拦截；潜在缺陷：用 trim 后长度而非原始长度导致边界漂移。
  it("ref 长度 > 4096 → ok:false 提示长度超限", () => {
    const longRef = "x".repeat(ATTACHMENT_MAX_REF_LEN + 1);
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths([longRef]);
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(true);
    });
    expect(validation.ok).toBe(false);
    expect(validation.message).toContain(String(ATTACHMENT_MAX_REF_LEN));
  });

  // 测试目的：url 协议非法（ftp://）拦截；潜在缺陷：未用 ^https?:// 正则导致 ftp 漏过。
  it("url 非 http(s)（ftp://） → ok:false 提示必须以 http(s):// 开头", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addUrl("ftp://example.com/x");
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(true);
    });
    expect(validation.ok).toBe(false);
    expect(validation.message).toContain("http(s)://");
  });

  // 测试目的：url 缺少协议（x.com）拦截；潜在缺陷：宽松匹配把裸域名当合法。
  it("url 缺少协议（x.com） → ok:false", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addUrl("x.com");
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(true);
    });
    expect(validation.ok).toBe(false);
  });

  // 测试目的：合法 https url 通过；潜在缺陷：大小写 HTTPS 被误判。
  it("合法 https URL → ok:true", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addUrl("HTTPS://example.com/x");
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(true);
    });
    expect(validation.ok).toBe(true);
  });

  // 测试目的：file/directory/image 不受 url 协议校验影响（非 url 类型 ref 任意合法）。
  it("file 类型长路径（非 url）不触发协议校验 → ok:true", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addDirectoryPath("C:\\some\\normal\\path");
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(true);
    });
    expect(validation.ok).toBe(true);
  });
});

describe("validateForSend 规则 3：模型视觉能力拦截", () => {
  beforeEach(() => vi.clearAllMocks());

  // 测试目的：supports_image=false 且含 image 附件 → 拦截；潜在缺陷：undefined 被当 false 误拦。
  it("supports_image=false 且含 image → ok:false 提示不支持图片", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(["/shot.png"]);
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(false);
    });
    expect(validation.ok).toBe(false);
    expect(validation.message).toContain("不支持图片");
  });

  // 测试目的：supports_image=true 且含 image → 通过；潜在缺陷：true 被误判为未声明。
  it("supports_image=true 且含 image → ok:true", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(["/shot.png"]);
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(true);
    });
    expect(validation.ok).toBe(true);
  });

  // 测试目的：supports_image=undefined 且含 image → 不拦截（「明确为 false 才拦截」语义）。
  it("supports_image=undefined 且含 image → ok:true（未声明不拦截）", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(["/shot.png"]);
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(undefined);
    });
    expect(validation.ok).toBe(true);
  });

  // 测试目的：supports_image=false 但不含 image（仅 file/url）→ 不拦截；潜在缺陷：误把 file 当 image。
  it("supports_image=false 但仅 file/url → ok:true（不误拦）", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths(["/notes.md"]);
      result.current.addUrl("https://example.com");
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(false);
    });
    expect(validation.ok).toBe(true);
  });
});

describe("validateForSend 空附件与多规则短路顺序", () => {
  beforeEach(() => vi.clearAllMocks());

  // 测试目的：无附件（空数组）发送合法；潜在缺陷：空数组被当「非空校验失败」。
  it("空附件列表 → ok:true", () => {
    const { result } = renderHook(() => useAttachmentInput());
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(false);
    });
    expect(validation.ok).toBe(true);
  });

  // 测试目的：同时数量超限+含非法 url，应优先返回数量错误（短路顺序稳定）。
  it("同时触发多规则 → 返回首个（数量）错误", () => {
    const { result } = renderHook(() => useAttachmentInput());
    act(() => {
      result.current.addLocalFilePaths([
        ...Array.from({ length: 21 }, (_, i) => `/f${i}.txt`),
        "ftp://bad",
      ]);
    });
    let validation!: ReturnType<typeof result.current.validateForSend>;
    act(() => {
      validation = result.current.validateForSend(false);
    });
    expect(validation.ok).toBe(false);
    expect(validation.message).toContain(String(ATTACHMENT_MAX_COUNT));
  });
});
