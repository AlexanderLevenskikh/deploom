#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

CYRILLIC = re.compile(r"[А-Яа-яЁё]")

APP_REPLACEMENTS = {
    "aria-label={text('Тема интерфейса', 'Interface theme')}":
        "aria-label={t('app.theme.interface')}",
    "title={text('Светлая тема', 'Light theme')}":
        "title={t('app.theme.light')}",
    "title={text('Системная тема', 'System theme')}":
        "title={t('app.theme.system')}",
    "title={text('Тёмная тема', 'Dark theme')}":
        "title={t('app.theme.dark')}",
}

EN_KEYS = (
    '  "app.theme.interface": "Interface theme",\n'
    '  "app.theme.light": "Light theme",\n'
    '  "app.theme.system": "System theme",\n'
    '  "app.theme.dark": "Dark theme",\n'
)

RU_KEYS = (
    '  "app.theme.interface": "Тема интерфейса",\n'
    '  "app.theme.light": "Светлая тема",\n'
    '  "app.theme.system": "Системная тема",\n'
    '  "app.theme.dark": "Тёмная тема",\n'
)

def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".i18n-fix.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)

def patch_app(text: str) -> str:
    for old, new in APP_REPLACEMENTS.items():
        if old in text:
            text = text.replace(old, new, 1)
        elif new not in text:
            raise RuntimeError(f"APP_THEME_ANCHOR_NOT_FOUND: {old}")

    # Block P added `text` only for the theme switch in App.tsx.
    # Once the theme labels use typed locale keys, remove the unused helper.
    if "text(" not in text:
        text = text.replace(
            "const { language, setLanguage, t, text } = useLanguage()",
            "const { language, setLanguage, t } = useLanguage()",
            1,
        )

    match = CYRILLIC.search(text)
    if match:
        line = text.count("\n", 0, match.start()) + 1
        snippet = text.splitlines()[line - 1].strip()
        raise RuntimeError(
            f"APP_STILL_CONTAINS_CYRILLIC: line={line}: {snippet}"
        )
    return text

def add_keys(text: str, keys: str, *, locale: str) -> str:
    if '"app.theme.interface"' in text:
        for key in (
            '"app.theme.interface"',
            '"app.theme.light"',
            '"app.theme.system"',
            '"app.theme.dark"',
        ):
            if key not in text:
                raise RuntimeError(f"{locale.upper()}_PARTIAL_THEME_KEYS: missing {key}")
        return text

    anchor = (
        '  "app.interfaceLanguage": "Interface language",\n'
        if locale == "en"
        else '  "app.interfaceLanguage": "Язык интерфейса",\n'
    )
    if anchor not in text:
        raise RuntimeError(f"{locale.upper()}_I18N_ANCHOR_NOT_FOUND")
    return text.replace(anchor, anchor + keys, 1)

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    app_path = repo / "desktop" / "src" / "App.tsx"
    en_path = repo / "desktop" / "src" / "i18n" / "locales" / "en.ts"
    ru_path = repo / "desktop" / "src" / "i18n" / "locales" / "ru.ts"

    for path in (app_path, en_path, ru_path):
        if not path.is_file():
            raise SystemExit(f"I18N_THEME_FIX_FILE_NOT_FOUND: {path}")

    app = patch_app(app_path.read_text(encoding="utf-8"))
    en = add_keys(en_path.read_text(encoding="utf-8"), EN_KEYS, locale="en")
    ru = add_keys(ru_path.read_text(encoding="utf-8"), RU_KEYS, locale="ru")

    if args.dry_run:
        print("BLOCK_P_THEME_I18N_DRY_RUN_PASS")
        print("changes=App.tsx,en.ts,ru.ts")
        return

    atomic_write(app_path, app)
    atomic_write(en_path, en)
    atomic_write(ru_path, ru)
    print("BLOCK_P_THEME_I18N_APPLIED")
    print("changes=App.tsx,en.ts,ru.ts")

if __name__ == "__main__":
    main()
