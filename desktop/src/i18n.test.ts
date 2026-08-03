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
});
