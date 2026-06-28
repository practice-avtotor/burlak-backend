#!/usr/bin/env python3
"""
Скрипт для генерации графа зависимостей (импортов) проекта burlak-backend.
Генерирует Mermaid-диаграмму, которую можно открыть в VS Code с preview.

Использование:
    python scripts/generate_dep_graph.py

Результат: docs/project_dependency_graph.md
"""

import ast
import os
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = PROJECT_ROOT / "app"
BURLAK_PARSER_DIR = PROJECT_ROOT / "burlak_parser"
OUTPUT_FILE = PROJECT_ROOT / "docs" / "project_dependency_graph.md"

# Модули, которые не нужно детализировать (стандартная библиотека, внешние пакеты)
EXTERNAL_MODULES = {
    "fastapi", "uvicorn", "sqlalchemy", "celery", "redis",
    "pydantic", "typing", "abc", "enum", "datetime", "uuid",
    "pathlib", "os", "sys", "json", "logging", "io", "re",
    "hashlib", "shutil", "tempfile", "zipfile", "openpyxl",
    "lxml", "PIL", "numpy", "pandas", "httpx", "asyncio",
    "concurrent", "functools", "itertools", "collections",
    "dataclasses", "warnings", "contextlib", "copy",
    "decimal", "math", "operator", "pickle", "pprint",
    "queue", "random", "statistics", "string", "textwrap",
    "time", "traceback", "types", "weakref",
    # test-related
    "pytest", "unittest", "hypothesis",
}

def get_python_files(directory: Path) -> list[Path]:
    """Рекурсивно собирает все .py файлы в директории."""
    py_files = []
    for root, dirs, files in os.walk(directory):
        # Пропускаем __pycache__
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                py_files.append(Path(root) / f)
    return sorted(py_files)

def get_module_name(filepath: Path, base_dir: Path) -> str:
    """Преобразует путь к файлу в имя модуля Python."""
    rel_path = filepath.relative_to(base_dir.parent)  # поднимаемся на уровень выше burlak-backend/
    parts = list(rel_path.with_suffix("").parts)
    # Убираем 'burlak-backend' из начала
    if parts[0] == "burlak-backend":
        parts = parts[1:]
    return ".".join(parts)

def extract_imports(filepath: Path) -> set[str]:
    """Извлекает все импорты из файла с помощью AST."""
    imports = set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(filepath))
    except (SyntaxError, UnicodeDecodeError):
        return imports

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return imports

def is_internal_module(module_name: str) -> bool:
    """Проверяет, является ли модуль внутренним (app, burlak_parser)."""
    return module_name.startswith("app") or module_name.startswith("burlak_parser")

def build_dependency_graph() -> dict[str, set[str]]:
    """Строит граф зависимостей: модуль -> {зависимости}."""
    graph: dict[str, set[str]] = defaultdict(set)

    # Собираем файлы из app/ и burlak_parser/
    all_files = []
    for base_dir in [APP_DIR, BURLAK_PARSER_DIR]:
        if base_dir.exists():
            all_files.extend(get_python_files(base_dir))

    # Также включаем корневые файлы (main.py, и т.д.)
    root_files = [f for f in PROJECT_ROOT.glob("*.py") if f.is_file()]
    all_files.extend(root_files)

    for filepath in all_files:
        module_name = get_module_name(filepath, PROJECT_ROOT)
        imports = extract_imports(filepath)

        for imp in imports:
            if is_internal_module(imp):
                graph[module_name].add(imp)

        if module_name not in graph:
            graph[module_name] = set()

    return graph

def group_modules(graph: dict[str, set[str]]) -> dict[str, list[str]]:
    """Группирует модули по слоям (app, burlak_parser)."""
    groups: dict[str, list[str]] = defaultdict(list)
    for module in graph:
        if module.startswith("app"):
            groups["app"].append(module)
        elif module.startswith("burlak_parser"):
            groups["burlak_parser"].append(module)
        else:
            groups["other"].append(module)
    return groups

def generate_mermaid_graph(graph: dict[str, set[str]]) -> str:
    """Генерирует Mermaid-диаграмму."""
    lines = []
    lines.append("```mermaid")
    lines.append("graph TD")
    lines.append("")

    # Определяем группы для стилизации
    groups = group_modules(graph)

    # Создаем узлы с группировкой (subgraph)
    lines.append("    %% --- Слой API ---")
    lines.append("    subgraph API_Layer[\"🌐 API Layer (app/api)\"]")
    api_modules = [m for m in groups["app"] if "api" in m]
    for mod in sorted(api_modules):
        label = mod.replace("app.", "").replace(".", "/")
        lines.append(f"        {mod}[{label}]")
    lines.append("    end")
    lines.append("")

    lines.append("    %% --- Слой Services ---")
    lines.append("    subgraph Service_Layer[\"⚙️ Service Layer (app/services)\"]")
    svc_modules = [m for m in groups["app"] if "services" in m]
    for mod in sorted(svc_modules):
        label = mod.replace("app.", "").replace(".", "/")
        lines.append(f"        {mod}[{label}]")
    lines.append("    end")
    lines.append("")

    lines.append("    %% --- Слой DB ---")
    lines.append("    subgraph DB_Layer[\"🗄️ DB Layer (app/db)\"]")
    db_modules = [m for m in groups["app"] if "db" in m]
    for mod in sorted(db_modules):
        label = mod.replace("app.", "").replace(".", "/")
        lines.append(f"        {mod}[{label}]")
    lines.append("    end")
    lines.append("")

    lines.append("    %% --- Слой Worker ---")
    lines.append("    subgraph Worker_Layer[\"⏳ Worker Layer (app/worker)\"]")
    worker_modules = [m for m in groups["app"] if "worker" in m]
    for mod in sorted(worker_modules):
        label = mod.replace("app.", "").replace(".", "/")
        lines.append(f"        {mod}[{label}]")
    lines.append("    end")
    lines.append("")

    lines.append("    %% --- Слой Core ---")
    lines.append("    subgraph Core_Layer[\"🔧 Core Layer (app/core)\"]")
    core_modules = [m for m in groups["app"] if "core" in m]
    for mod in sorted(core_modules):
        label = mod.replace("app.", "").replace(".", "/")
        lines.append(f"        {mod}[{label}]")
    lines.append("    end")
    lines.append("")

    lines.append("    %% --- Слой Schemas ---")
    lines.append("    subgraph Schema_Layer[\"📋 Schema Layer (app/schemas)\"]")
    schema_modules = [m for m in groups["app"] if "schemas" in m]
    for mod in sorted(schema_modules):
        label = mod.replace("app.", "").replace(".", "/")
        lines.append(f"        {mod}[{label}]")
    lines.append("    end")
    lines.append("")

    lines.append("    %% --- Парсер ---")
    lines.append("    subgraph Parser_Layer[\"📄 Parser (burlak_parser)\"]")
    parser_modules = [m for m in groups["burlak_parser"]]
    for mod in sorted(parser_modules):
        label = mod.replace("burlak_parser.", "")
        lines.append(f"        {mod}[{label}]")
    lines.append("    end")
    lines.append("")

    # Добавляем связи
    lines.append("    %% --- Зависимости ---")
    for source, targets in sorted(graph.items()):
        for target in sorted(targets):
            if source in graph and target in graph:
                lines.append(f"    {source} --> {target}")

    lines.append("")
    lines.append("    %% --- Стилизация ---")
    lines.append("    classDef api fill:#e1f5fe,stroke:#01579b;")
    lines.append("    classDef svc fill:#e8f5e9,stroke:#1b5e20;")
    lines.append("    classDef db fill:#fff3e0,stroke:#e65100;")
    lines.append("    classDef worker fill:#fce4ec,stroke:#880e4f;")
    lines.append("    classDef core fill:#f3e5f5,stroke:#4a148c;")
    lines.append("    classDef schema fill:#e0f2f1,stroke:#004d40;")
    lines.append("    classDef parser fill:#fff8e1,stroke:#f57f17;")
    lines.append("")

    # Применяем классы
    for mod in api_modules:
        lines.append(f"    class {mod} api;")
    for mod in svc_modules:
        lines.append(f"    class {mod} svc;")
    for mod in db_modules:
        lines.append(f"    class {mod} db;")
    for mod in worker_modules:
        lines.append(f"    class {mod} worker;")
    for mod in core_modules:
        lines.append(f"    class {mod} core;")
    for mod in schema_modules:
        lines.append(f"    class {mod} schema;")
    for mod in parser_modules:
        lines.append(f"    class {mod} parser;")

    lines.append("```")
    return "\n".join(lines)

def generate_text_summary(graph: dict[str, set[str]]) -> str:
    """Генерирует текстовую сводку зависимостей."""
    lines = []
    lines.append("## Сводка зависимостей\n")
    lines.append("| Модуль | Зависит от |")
    lines.append("|--------|------------|")
    for source, targets in sorted(graph.items()):
        if targets:
            deps = ", ".join(sorted(targets))
            lines.append(f"| `{source}` | `{deps}` |")
        else:
            lines.append(f"| `{source}` | _(нет внутренних зависимостей)_ |")
    return "\n".join(lines)

def main():
    print("🔍 Анализируем импорты в проекте...")
    graph = build_dependency_graph()

    print(f"📦 Найдено модулей: {len(graph)}")
    for mod, deps in sorted(graph.items()):
        if deps:
            print(f"   {mod} -> {', '.join(sorted(deps))}")

    print("\n📊 Генерируем Mermaid-диаграмму...")
    mermaid = generate_mermaid_graph(graph)
    summary = generate_text_summary(graph)

    # Собираем итоговый документ
    content = f"""# Граф зависимостей проекта burlak-backend

> Автоматически сгенерировано {__file__}
> Дата: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}

## Mermaid-диаграмма

{mermaid}

{summary}
"""
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(content, encoding="utf-8")
    print(f"\n✅ Граф сохранён: {OUTPUT_FILE}")
    print("💡 Откройте файл в VS Code и нажмите Ctrl+Shift+V для preview")

if __name__ == "__main__":
    main()