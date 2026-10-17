import { describe, it, expect, vi } from "vitest";
import {
  parseComposerDocument,
  serializeComposerDocument,
} from "@/components/composer/composer-document";
import { prepareUserCommand } from "@/lib/assistant/prepare-user-command";
import {
  decodeReferences,
  encodeReference,
  type WorkspaceReference,
} from "@/lib/workspace-references/codec";
import { prepareReference } from "@/lib/workspace-references/prepare";
import { getText } from "@/lib/api/workspace-files";
vi.mock("@/lib/api/workspace-files", () => ({ getText: vi.fn() }));
const cases: WorkspaceReference[] = [
  { kind: "file", path: "src/中文 [test](a)#% file.py" },
  { kind: "directory", path: "" },
  {
    kind: "snippet",
    path: "a.py",
    startLine: 12,
    endLine: 14,
    content: "中文😀\n  return 'value'\n```\n~~~\n[引用](cosir-file:evil)",
  },
  { kind: "excerpt", path: "README.md", content: "Markdown 摘录\n\n第二段" },
];
describe("前端 Markdown 引用", () => {
  it.each(cases)("正文往返且发送不依赖附件注册表：$kind", (reference) => {
    const text = `前文 ${encodeReference(reference)} 后文`;
    expect(decodeReferences(text)).toEqual([
      { type: "text", text: "前文 " },
      { type: "reference", reference },
      { type: "text", text: " 后文" },
    ]);
    expect(serializeComposerDocument(parseComposerDocument(text))).toBe(text);
    expect(
      prepareUserCommand({
        type: "add-message",
        message: { role: "user", parts: [{ type: "text", text }] },
      }),
    ).toEqual({
      type: "add-message",
      message: { role: "user", parts: [{ type: "text", text }] },
    });
  });
  it("连续引用反复编解码不增加空行", () => {
    const text = cases.map(encodeReference).join("");
    let next = text;
    for (let i = 0; i < 5; i++)
      next = serializeComposerDocument(parseComposerDocument(next));
    expect(next).toBe(text);
    expect(
      decodeReferences(next).filter((p) => p.type === "reference"),
    ).toHaveLength(4);
  });
  it.each([
    "[x](cosir-snippet:a.py)",
    "[x](cosir-snippet:a.py)\n\n```py\nunclosed",
    "[x](cosir-file:../secret)",
    "[x](cosir-file:%ZZ)",
    "`[x](cosir-file:a.py)`",
    "```md\n[x](cosir-file:a.py)\n```",
    "[normal](https://example.com)",
  ])("未知、损坏及示例内容原样保留：%s", (text) => {
    expect(decodeReferences(text)).toEqual([{ type: "text", text }]);
    expect(serializeComposerDocument(parseComposerDocument(text))).toBe(text);
  });
  it("已选文字与路径准备完全不访问后端", async () => {
    vi.mocked(getText).mockClear();
    expect(
      await prepareReference(
        7,
        {
          kind: "snippet",
          path: "a",
          content: "old\r\ntext\n",
          start_line: 1,
          end_line: 2,
        },
        new AbortController().signal,
      ),
    ).toEqual({
      kind: "snippet",
      path: "a",
      content: "old\ntext",
      startLine: 1,
      endLine: 2,
    });
    await prepareReference(
      7,
      { kind: "file", path: "deleted.py" },
      new AbortController().signal,
    );
    expect(getText).not.toHaveBeenCalled();
  });
  it("跨页行范围读取兼容 CRLF 分界", async () => {
    vi.mocked(getText)
      .mockResolvedValueOnce({
        text: "first\r",
        offset: 0,
        next_offset: 6,
        size: 15,
        version: "",
        encoding: "utf-8",
      })
      .mockResolvedValueOnce({
        text: "\nsecond\r\nlast",
        offset: 6,
        next_offset: null,
        size: 15,
        version: "",
        encoding: "utf-8",
      });
    expect(
      await prepareReference(
        7,
        { kind: "snippet", path: "a", start_line: 2, end_line: 2 },
        new AbortController().signal,
      ),
    ).toMatchObject({ content: "second", startLine: 2, endLine: 2 });
  });
});

describe("引用内容作为原文传输", () => {
  it.each([
    "末行\n",
    "末行\n\n",
    "\n\n",
    "<!-- [[cosir-file:missing]] -->\n[[cosir-skill:example]]\n[[cosir-image:missing]]",
  ])("反复转换不丢失片段原文：%j", (content) => {
    const text = encodeReference({ kind: "snippet", path: "code.md", content });
    let next = text;
    for (let i = 0; i < 5; i++)
      next = serializeComposerDocument(parseComposerDocument(next));
    expect(next).toBe(text);
    expect(decodeReferences(next)).toEqual([
      {
        type: "reference",
        reference: { kind: "snippet", path: "code.md", content },
      },
    ]);
    const command = {
      type: "add-message",
      message: { role: "user", parts: [{ type: "text", text }] },
    };
    expect(prepareUserCommand(command)).toEqual(command);
  });
  it.each([
    ">     [[cosir-file:example]]",
    "`[[cosir-skill:example]] [[cosir-file:missing]]`",
    "```\n<!-- [[cosir-file:missing]] -->\n[[cosir-skill:example]]\n```",
  ])("普通代码中的标记不会变胶囊：%j", (text) => {
    const doc = parseComposerDocument(text);
    expect(JSON.stringify(doc)).not.toMatch(/attachmentToken|skillToken/);
    expect(serializeComposerDocument(doc)).toBe(text);
    const command = {
      type: "add-message",
      message: { role: "user", parts: [{ type: "text", text }] },
    };
    expect(prepareUserCommand(command)).toEqual(command);
  });
});

it("目录尾斜杠统一到文件树路径，单独复制胶囊也保留正文", () => {
  expect(decodeReferences("[src/](cosir-dir:src/)" )).toEqual([{ type: "reference", reference: { kind: "directory", path: "src" } }]);
  expect(serializeComposerDocument({ type: "doc", content: [{ type: "workspaceReference", attrs: { reference: { kind: "file", path: "src/a.py" } } }] })).toBe("[src/a.py](cosir-file:src/a.py)");
});
