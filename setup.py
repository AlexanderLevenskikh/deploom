from pathlib import Path
from setuptools import setup

root = Path(__file__).resolve().parent
modules = sorted(p.stem for p in root.glob("*.py") if p.stem != "setup")
setup(
    py_modules=modules,
    data_files=[
        ("share/deploom-core", ["VERSION", "settings.schema.json", "dependency-knowledge.schema.json"]),
        ("share/deploom-core/document_locales", ["document_locales/en.json", "document_locales/ru.json"]),
    ],
)
