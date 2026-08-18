export type Language = 'en' | 'zh'

type LanguageMetadata = {
  title: string
  description: string
  ogTitle: string
  ogDescription: string
}

const metadata: Record<Language, LanguageMetadata> = {
  en: {
    title: 'OpenThesis — Research companies, not prices',
    description: 'OpenThesis — research companies, not prices. Official evidence, deterministic calculations and focused AI research in one local-first workbench.',
    ogTitle: 'OpenThesis — Research companies, not prices',
    ogDescription: 'Official evidence, deterministic calculations and focused AI research in one local-first workbench.'
  },
  zh: {
    title: 'OpenThesis — 研究公司，而不是追逐价格',
    description: 'OpenThesis——研究公司，而不是追逐价格。把官方证据、确定性计算和专注的 AI 研究组织到一个本地优先的工作台中。',
    ogTitle: 'OpenThesis — 研究公司，而不是追逐价格',
    ogDescription: '官方证据、确定性计算和专注的 AI 研究，汇聚于一个本地优先的工作台。'
  }
}

export const LANGUAGE_STORAGE_KEY = 'openthesis-site-language'

export function languageFromNavigator(languages: readonly string[] = []): Language {
  return languages.some((language) => language.toLowerCase().startsWith('zh')) ? 'zh' : 'en'
}

export function storedLanguage(): Language | null {
  try {
    const stored = window.localStorage.getItem(LANGUAGE_STORAGE_KEY)
    return stored === 'en' || stored === 'zh' ? stored : null
  } catch {
    return null
  }
}

export function resolveInitialLanguage(): Language {
  const stored = storedLanguage()
  if (stored) return stored
  const languages = typeof navigator === 'undefined'
    ? []
    : [...(navigator.languages ?? []), navigator.language ?? 'en']
  return languageFromNavigator(languages)
}

export function resolveSystemLanguage(): Language {
  const languages = typeof navigator === 'undefined'
    ? []
    : [...(navigator.languages ?? []), navigator.language ?? 'en']
  return languageFromNavigator(languages)
}

function setMetaContent(selector: string, content: string) {
  const meta = document.querySelector<HTMLMetaElement>(selector)
  if (meta) meta.content = content
}

export function applyLanguageMetadata(language: Language) {
  const content = metadata[language]
  document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en'
  document.title = content.title
  setMetaContent('meta[name="description"]', content.description)
  setMetaContent('meta[property="og:title"]', content.ogTitle)
  setMetaContent('meta[property="og:description"]', content.ogDescription)
}
