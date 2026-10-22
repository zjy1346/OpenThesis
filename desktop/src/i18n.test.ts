import { describe, expect, it } from "vitest";

import { COPY } from "./i18n";

describe("desktop localization catalog", () => {
  it("keeps the Chinese and English interfaces structurally identical", () => {
    expect(Object.keys(COPY.en).sort()).toEqual(Object.keys(COPY["zh-CN"]).sort());
  });

  it("contains no blank interface copy", () => {
    for (const catalog of Object.values(COPY)) {
      const values = Object.values(catalog).flatMap((value) =>
        typeof value === "string" ? [value] : Object.values(value as Record<string, string>),
      );
      expect(values.every((value) => value.trim().length > 0)).toBe(true);
    }
  });

  it("contains a complete Traditional Chinese catalog", () => {
    expect(Object.keys(COPY["zh-Hant"]).sort()).toEqual(Object.keys(COPY["zh-CN"]).sort());
    expect(COPY["zh-Hant"].settings).toContain("\u8a2d\u5b9a");
    expect(COPY["zh-Hant"].visionFallback).toContain("\u8996\u89ba");
  });

  it("explains the confirmed local-database recovery boundary in every language", () => {
    for (const locale of ["zh-CN", "zh-Hant", "en"] as const) {
      expect(COPY[locale].storageRecoveryRequired).toContain("OpenThesis");
      expect(COPY[locale].storageRecoveryRequired).toMatch(/copy|复制|複製/);
      expect(COPY[locale].storageRecoveryRequired).toMatch(/backup|备份|備份/);
      expect(COPY[locale].storageRecoveryRequired).toMatch(/diagnostic|诊断|診斷/);
      expect(COPY[locale].storageRecoveryRequired).toMatch(/not|不会|不會/);
    }
  });

  it("does not claim a capacity failure necessarily submitted all material or requires a larger model", () => {
    expect(COPY["zh-CN"].synthesisCapacityBody).not.toContain("完整材料已提交");
    expect(COPY["zh-Hant"].synthesisCapacityBody).not.toContain("完整資料已提交");
    expect(COPY.en.synthesisCapacityBody).not.toContain("The full material was submitted");
    expect(COPY["zh-CN"].synthesisCapacityModel).not.toContain("更大");
    expect(COPY["zh-Hant"].synthesisCapacityModel).not.toContain("更大");
    expect(COPY.en.synthesisCapacityModel).not.toContain("Larger");
  });

  it("localizes both vision approval modes and their scope in all supported languages", () => {
    for (const locale of ["zh-CN", "zh-Hant", "en"] as const) {
      expect(COPY[locale].visionApprovalMode.trim()).not.toBe("");
      expect(COPY[locale].visionApprovalEach.trim()).not.toBe("");
      expect(COPY[locale].visionApprovalResearch.trim()).not.toBe("");
      expect(COPY[locale].visionApprovalScope).toContain("20");
    }
  });

  it("contains the financial diagnostics copy in all supported languages", () => {
    for (const locale of ["zh-CN", "zh-Hant", "en"] as const) {
      expect(COPY[locale].financialExportDiagnostics.trim()).not.toBe("");
      expect(COPY[locale].financialFailedReports.trim()).not.toBe("");
      expect(COPY[locale].financialFailedFields.trim()).not.toBe("");
      expect(COPY[locale].financialConfigureCloud.trim()).not.toBe("");
    }
  });

  it("does not fall back to common Simplified-only UI glyphs", () => {
    const simplifiedOnly = new Set("\u8bbe\u62a5\u8d22\u52a1\u8bed\u7edc\u8fdb\u53d1\u8bc1\u9690\u8fb9\u9645\u73b0\u95ee\u9898\u4ea7\u5f00\u5173\u8bf7\u8bb8\u5355\u4ece\u5bf9\u4e3a\u957f\u7ed3\u95f4\u8fd8\u5c06\u5f53\u8ba9");
    const leaked = Object.values(COPY["zh-Hant"])
      .flatMap((value) => typeof value === "string" ? [...value] : Object.values(value as Record<string, string>).flatMap((nested) => [...nested]))
      .filter((char) => simplifiedOnly.has(char));
    expect(new Set(leaked)).toEqual(new Set());
  });
});
