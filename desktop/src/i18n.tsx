import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

export type Language = 'ru' | 'en'

type LanguageContextValue = {
  language: Language
  setLanguage: (language: Language) => void
  text: (ru: string, en: string) => string
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
  // Preserve the current product behaviour for existing users.
  return 'ru'
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
    text: (ru, en) => language === 'ru' ? ru : en,
  }), [language])

  return <LanguageContext.Provider value={value}>{children}</LanguageContext.Provider>
}

export function useLanguage(): LanguageContextValue {
  const value = useContext(LanguageContext)
  if (!value) throw new Error('useLanguage must be used inside LanguageProvider')
  return value
}