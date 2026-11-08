"use client";

import { createContext, useContext, useEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { defaultLocale, isLocale, localizedDate, localeStorageKey, readLocale, translate, writeLocale, type DatePrecision, type Locale, type TranslationValues } from "./i18n";

type LanguageState = { locale: Locale; error: string };
const initialState: LanguageState = { locale: defaultLocale, error: "" };
let state = initialState;
const listeners = new Set<() => void>();

function publish(next: LanguageState) {
  if (next.locale === state.locale && next.error === state.error) return;
  state = next;
  listeners.forEach(listener => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

function selectLocale(value: string) {
  if (!isLocale(value)) {
    publish({ ...state, error: "Unsupported interface language." });
    return;
  }
  try {
    writeLocale(window.localStorage, value);
    publish({ locale: value, error: "" });
  } catch {
    publish({ locale: value, error: "Language preference could not be saved. Your selection applies only to this page." });
  }
}

type I18n = {
  locale: Locale;
  error: string;
  setLocale: (locale: string) => void;
  t: (message: string, values?: TranslationValues) => string;
  date: (value?: number | null, precision?: DatePrecision) => string;
};
const LocaleContext = createContext<I18n | null>(null);

export function LocaleProvider({ children }: { children: ReactNode }) {
  const current = useSyncExternalStore(subscribe, () => state, () => initialState);
  useEffect(() => {
    try {
      publish({ locale: readLocale(window.localStorage), error: "" });
    } catch {
      publish({ ...state, error: "Language preference could not be restored. English is used until you choose a language." });
    }
    function onStorage(event: StorageEvent) {
      if (event.key === localeStorageKey || event.key === null) {
        publish({ locale: isLocale(event.newValue) ? event.newValue : defaultLocale, error: "" });
      }
    }
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
  useEffect(() => {
    document.documentElement.lang = current.locale;
  }, [current.locale]);
  const value = useMemo<I18n>(() => ({
    ...current,
    setLocale: selectLocale,
    t: (message, values) => translate(message, current.locale, values),
    date: (value, precision) => localizedDate(value, current.locale, precision),
  }), [current]);
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useI18n(): I18n {
  const context = useContext(LocaleContext);
  if (!context) throw new Error("UI localization requires LocaleProvider.");
  return context;
}

export function LanguageSelector({ id = "interface-language" }: { id?: string }) {
  const { locale, setLocale, error, t } = useI18n();
  return <div className="language-selector">
    <label htmlFor={id}>Language / 语言</label>
    <select id={id} aria-label={t("Language")} value={locale} onChange={event => setLocale(event.target.value)}>
      <option value="en" lang="en">English</option>
      <option value="zh-CN" lang="zh-CN">简体中文</option>
    </select>
    {error && <p className="language-error" role="alert">{t(error)}</p>}
  </div>;
}
