import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { en, type TranslationKey } from './i18n/locales/en'
import { ru } from './i18n/locales/ru'

export type Language = 'ru' | 'en'
export type TranslationValues = Record<string, string | number>

type LanguageContextValue = {
  language: Language
  setLanguage: (language: Language) => void
  t: (key: TranslationKey, values?: TranslationValues) => string
  text: (ruText: string, enText: string) => string
}

const STORAGE_KEY = 'deploom.language'
const LanguageContext = createContext<LanguageContextValue | undefined>(undefined)

function initialLanguage(): Language {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY)
    if (saved === 'ru' || saved === 'en') return saved
  } catch {
    // Local storage can be unavailable in hardened environments.
  }
  return 'en'
}

function interpolate(template: string, values?: TranslationValues): string {
  if (!values) return template
  return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (match, key: string) => {
    const value = values[key]
    return value === undefined ? match : String(value)
  })
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<Language>(initialLanguage)

  useEffect(() => {
    document.documentElement.lang = language
    try {
      window.localStorage.setItem(STORAGE_KEY, language)
    } catch {
      // Non-critical preference persistence.
    }
  }, [language])

  const value = useMemo<LanguageContextValue>(() => ({
    language,
    setLanguage,
    t: (key, values) => interpolate((language === 'ru' ? ru : en)[key], values),
    // Compatibility bridge for highly dynamic copy while the canonical static
    // UI copy lives in typed locale dictionaries.
    text: (ruText, enText) => language === 'ru' ? ruText : enText,
  }), [language])

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
}

export function useLanguage(): LanguageContextValue {
  const value = useContext(LanguageContext)
  if (!value) throw new Error('useLanguage must be used inside LanguageProvider')
  return value
}

export type { TranslationKey }
