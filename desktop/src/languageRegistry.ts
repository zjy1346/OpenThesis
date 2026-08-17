import type { Language, UiLanguageMode } from "./types";
import contract from "../../language-contract.json";

export type LanguageDefinition = {
  id: Language;
  aliases: readonly string[];
  localePrefixes: readonly string[];
  htmlLang: string;
  dir: "ltr" | "rtl";
};

export const LANGUAGE_REGISTRY: readonly LanguageDefinition[] = [
  { id: "zh-CN", aliases: ["zh-cn", "zh-hans", "zh-hans-cn", "zh-sg", "zh"], localePrefixes: ["zh-hans", "zh-cn", "zh-sg", "zh"], htmlLang: "zh-CN", dir: "ltr" },
  { id: "zh-Hant", aliases: ["zh-hant", "zh-tw", "zh-hk", "zh-mo", "zh-hant-tw", "zh-hant-hk"], localePrefixes: ["zh-hant", "zh-tw", "zh-hk", "zh-mo"], htmlLang: "zh-Hant", dir: "ltr" },
  { id: "en", aliases: ["en", "en-us", "en-gb"], localePrefixes: ["en"], htmlLang: "en", dir: "ltr" },
];

export const LANGUAGE_CONTRACT = contract;

export function languageOptions(registry: readonly LanguageDefinition[] = LANGUAGE_REGISTRY): readonly LanguageDefinition[] {
  return registry;
}

export function normalizeLanguage(value: string | undefined | null): Language {
  const normalized = (value ?? "").trim().toLowerCase().replaceAll("_", "-");
  for (const definition of [LANGUAGE_REGISTRY[1], LANGUAGE_REGISTRY[0], LANGUAGE_REGISTRY[2]]) {
    if (definition.aliases.includes(normalized) || definition.localePrefixes.some((prefix) => normalized.startsWith(`${prefix}-`))) return definition.id;
  }
  return "zh-CN";
}

export function resolveSystemLanguage(locales: readonly string[] = []): Language {
  for (const locale of locales) {
    const normalized = locale.trim().toLowerCase().replaceAll("_", "-");
    if (normalized.startsWith("zh-hant") || ["zh-tw", "zh-hk", "zh-mo"].includes(normalized)) return "zh-Hant";
    if (normalized.startsWith("zh")) return "zh-CN";
    if (normalized.startsWith("en")) return "en";
  }
  return "en";
}

export function resolveUiLanguage(mode: UiLanguageMode | undefined, stored: string | undefined, locales: readonly string[] = []): Language {
  return mode === "system" ? resolveSystemLanguage(locales) : normalizeLanguage(stored);
}

export function languageName(language: Language, displayLanguage: Language = language): string {
  const names: Record<Language, Record<Language, string>> = {
    "zh-CN": { "zh-CN": "简体中文", "zh-Hant": "簡體中文", en: "Simplified Chinese" },
    "zh-Hant": { "zh-CN": "繁體中文", "zh-Hant": "繁體中文", en: "Traditional Chinese" },
    en: { "zh-CN": "英文", "zh-Hant": "英文", en: "English" },
  };
  return names[language][displayLanguage];
}

export function setDocumentLanguage(language: Language): void {
  if (typeof document === "undefined") return;
  const definition = LANGUAGE_REGISTRY.find((item) => item.id === language) ?? LANGUAGE_REGISTRY[0];
  document.documentElement.lang = definition.htmlLang;
  document.documentElement.dir = definition.dir;
}
