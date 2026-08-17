import { describe, expect, it } from "vitest";
import { LANGUAGE_CONTRACT, LANGUAGE_REGISTRY, languageName, languageOptions, normalizeLanguage, resolveSystemLanguage, resolveUiLanguage } from "./languageRegistry";
import { COPY } from "./i18n";
import type { Language } from "./types";

describe("language registry", () => {
  it("normalizes aliases and safely falls back", () => {
    expect(normalizeLanguage("zh-TW")).toBe("zh-Hant");
    expect(normalizeLanguage("zh_Hant_HK")).toBe("zh-Hant");
    expect(normalizeLanguage("zh-Hans-SG")).toBe("zh-CN");
    expect(normalizeLanguage("x-klingon")).toBe("zh-CN");
  });
  it("resolves ordered system locales and manual precedence", () => {
    expect(resolveSystemLanguage(["fr-FR", "zh-HK"])).toBe("zh-Hant");
    expect(resolveSystemLanguage(["fr-FR"])).toBe("en");
    expect(resolveUiLanguage("manual", "en", ["zh-TW"])).toBe("en");
    expect(resolveUiLanguage("system", "en", ["zh-TW"])).toBe("zh-Hant");
    expect(languageName("zh-Hant", "en")).toBe("Traditional Chinese");
  });
  it("matches the shared contract and supports a virtual fourth-language option", () => {
    expect(LANGUAGE_CONTRACT.fallback).toBe("en");
    expect(LANGUAGE_CONTRACT.unknown_legacy_fallback).toBe("zh-CN");
    expect(LANGUAGE_CONTRACT.languages.map((item) => item.id)).toEqual(["zh-CN", "zh-Hant", "en"]);
    expect(LANGUAGE_CONTRACT.languages.map((item) => item.localePrefixes)).toEqual(LANGUAGE_REGISTRY.map((item) => item.localePrefixes));
    for (const item of LANGUAGE_CONTRACT.languages) {
      for (const display of ["zh-CN", "zh-Hant", "en"] as const) {
        expect(item.names[display]).toBe(languageName(item.id as Language, display));
      }
    }
    const virtual = { id: "x-test" as "en", aliases: ["x-test"], localePrefixes: ["x-test"], htmlLang: "x-test", dir: "ltr" as const };
    expect(languageOptions([virtual]).map((item) => item.id)).toEqual(["x-test"]);
  });
  it("keeps the Traditional Chinese catalog key-complete", () => {
    expect(Object.keys(COPY["zh-Hant"]).sort()).toEqual(Object.keys(COPY.en).sort());
  });
});
