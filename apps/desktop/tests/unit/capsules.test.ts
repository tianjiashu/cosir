import { describe, expect, it } from "vitest";

import fixture from "@/lib/assistant/capsules/contract/capsule-grammar-cases.json";
import {
  capsuleTextToPlainText,
  parseCapsules,
  serializeCapsule,
  type CapsuleSegment,
} from "@/lib/assistant/capsules";

const canonicalOf = (segments: CapsuleSegment[]) =>
  segments
    .map((segment) =>
      segment.type === "text" ? segment.text : serializeCapsule(segment.capsule),
    )
    .join("");

describe("胶囊文本语法", () => {
  for (const item of fixture.cases) {
    it(item.name, () => {
      const segments = parseCapsules(item.text);
      expect(segments).toEqual(item.segments);
      expect(capsuleTextToPlainText(item.text)).toBe(item.plain);
      expect(canonicalOf(segments)).toBe(item.canonical);
    });
  }

  for (const text of fixture.notCapsules) {
    it(`语法不合法保持普通文本：${text}`, () => {
      expect(parseCapsules(text)).toEqual([{ type: "text", text }]);
      expect(capsuleTextToPlainText(text)).toBe(text);
    });
  }
});
