import { describe, expect, it } from "vitest";

import { COPY } from "./i18n";

describe("desktop localization catalog", () => {
  it("keeps the Chinese and English interfaces structurally identical", () => {
    expect(Object.keys(COPY.en).sort()).toEqual(Object.keys(COPY["zh-CN"]).sort());
  });

  it("contains no blank interface copy", () => {
    for (const catalog of Object.values(COPY)) {
      expect(Object.values(catalog).every((value) => value.trim().length > 0)).toBe(true);
    }
  });

  it("contains a complete Traditional Chinese catalog", () => {
    expect(Object.keys(COPY["zh-Hant"]).sort()).toEqual(Object.keys(COPY["zh-CN"]).sort());
    expect(COPY["zh-Hant"].settings).toContain("\u8a2d\u5b9a");
    expect(COPY["zh-Hant"].visionFallback).toContain("\u8996\u89ba");
  });

  it("does not fall back to common Simplified-only UI glyphs", () => {
    const simplifiedOnly = new Set("\u8bbe\u62a5\u8d22\u52a1\u8bed\u7edc\u8fdb\u53d1\u8bc1\u9690\u8fb9\u9645\u73b0\u95ee\u9898\u4ea7\u5f00\u5173\u8bf7\u8bb8\u5355\u4ece\u5bf9\u4e3a\u957f\u7ed3\u95f4\u8fd8\u5c06\u5f53\u8ba9");
    const leaked = Object.values(COPY["zh-Hant"])
      .flatMap((value) => [...value])
      .filter((char) => simplifiedOnly.has(char));
    expect(new Set(leaked)).toEqual(new Set());
  });
});
