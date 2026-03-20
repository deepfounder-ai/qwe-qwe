# qwe-qwe — План улучшений продукта

## Overview
Комплексный план развития qwe-qwe из MVP в production-ready open-source агента, оптимизированного под Qwen 3.5 9B на GPU 8GB. Фокус: надёжность tool-calling, эффективность контекстного окна (32K), удобство установки и новые возможности (мультимодальность, RAG).

## Context
- **Type**: Enhancement + Refactor
- **Priority**: High
- **Estimated Complexity**: Complex
- **Affected Areas**: все модули проекта
- **Целевая модель**: Qwen 3.5 9B (32K context, 8GB VRAM)

---

## Фаза 1 — Стабильность и качество (фундамент)

### 1.1 Безопасность: .gitignore и vault
**Описание**: Закрыть дыры в безопасности — .env, IDE, конфиг-оверрайды.
**Файлы**: `.gitignore`, `config.py`
**Задачи**:
- [ ] Добавить в `.gitignore`: `.env`, `.env.*`, `.idea/`, `.vscode/`, `*.log`, `.DS_Store`
- [ ] Убедиться что `.vault_key` уже в `.gitignore` (да, есть)
- [ ] Создать `config.local.py` механизм — оверрайд без изменения `config.py`
- [ ] Добавить `.env` поддержку через `os.environ.get()` с fallback на defaults

### 1.2 JSON Repair для tool calls
**Описание**: Qwen 3.5 9B часто генерирует невалидный JSON в аргументах tool calls. Добавить repair-логику.
**Файлы**: `agent.py`
**Задачи**:
- [ ] Написать `_repair_json(raw: str) -> dict` — починка типичных ошибок:
  - Trailing commas: `{"a": 1,}` → `{"a": 1}`
  - Одинарные кавычки: `{'a': 1}` → `{"a": 1}`
  - Незакрытые скобки: `{"a": 1` → `{"a": 1}`
  - Экранирование переносов строк внутри значений
  - Комментарии в JSON
- [ ] Интегрировать в парсинг tool calls (line ~277) как fallback после `json.loads()`
- [ ] Добавить метрику: счётчик repaired vs failed tool calls в TurnResult
- [ ] Логировать raw JSON при ошибке парсинга для отладки

### 1.3 Сжатие системного промпта
**Описание**: Оптимизировать системный промпт для максимальной эффективности в 32K окне. Сейчас ~475 токенов промпт + ~1425 токенов tool definitions = ~1900 токенов overhead.
**Файлы**: `soul.py`, `tools.py`
**Задачи**:
- [ ] Сжать правила (rules 1-10) — убрать повторы, использовать нумерованный список
- [ ] Сделать system info опциональным (убрать cwd, venv — модель их не использует)
- [ ] Сократить tool examples до 3-4 самых критичных (убрать очевидные)
- [ ] Оптимизировать tool descriptions — каждое слово на счету для 9B модели
- [ ] Добавить token budget: если history > 70% контекста, урезать auto-context memories
- [ ] Цель: сократить system overhead на 25-30% (~500 токенов экономии)

### 1.4 Синхронизация requirements.txt
**Описание**: requirements.txt содержит 3 пакета, pyproject.toml — 8. Убрать дублирование.
**Файлы**: `requirements.txt`, `pyproject.toml`
**Задачи**:
- [ ] Удалить `requirements.txt` — pyproject.toml является single source of truth
- [ ] Обновить install.sh/setup.sh если ссылаются на requirements.txt

### 1.5 Config через environment
**Описание**: Убрать хардкод IP `192.168.0.49` — сделать конфигурацию через env-переменные.
**Файлы**: `config.py`, `install.sh`, `setup.sh`
**Задачи**:
- [ ] Все параметры config.py: `os.environ.get("QWE_LLM_URL", "http://localhost:1234/v1")`
- [ ] Default = localhost (не чужой IP)
- [ ] install.sh: автоопределение LM Studio (сканировать localhost:1234, 192.168.x.x:1234)
- [ ] setup.sh: то же самое
- [ ] Документировать env-переменные в README

---

## Фаза 2 — Автоопределение и онбординг

### 2.1 Auto-discovery LM Studio / Ollama
**Описание**: При первом запуске автоматически искать LLM-сервер в сети.
**Файлы**: `config.py` (новая функция), `server.py` (setup endpoint), `cli.py`
**Задачи**:
- [ ] Написать `discover_llm_server()`:
  - Проверить `localhost:1234` (LM Studio default)
  - Проверить `localhost:11434` (Ollama default)
  - Проверить `localhost:8080` (llama.cpp default)
  - Если нашёл — автоматически выбрать провайдер и модель
- [ ] Интегрировать в setup flow (web onboarding + CLI first run)
- [ ] Показать пользователю: "Найден LM Studio на localhost:1234, модель: qwen3.5-9b. Использовать?"
- [ ] Fallback: ручной ввод URL если автоопределение не сработало

### 2.2 Улучшение Web Onboarding
**Описание**: Сделать первый запуск максимально простым — wizard с автоопределением.
**Файлы**: `server.py`, `static/index.html`
**Задачи**:
- [ ] Setup wizard: шаг 1 — автопоиск LLM → шаг 2 — имя/язык → шаг 3 — готово
- [ ] Прогресс-бар при поиске LLM серверов
- [ ] Подсказки: "Установите LM Studio и загрузите модель Qwen 3.5 9B"
- [ ] Ссылка на инструкцию по установке LM Studio для каждой ОС

---

## Фаза 3 — Docker и простота запуска

### 3.1 Docker-контейнеризация
**Описание**: Один `docker compose up` для полного стека (qwe-qwe + Qdrant). LM Studio остаётся на хосте (GPU passthrough сложен).
**Файлы**: новые `Dockerfile`, `docker-compose.yml`
**Задачи**:
- [ ] `Dockerfile`: Python 3.11-slim, pip install, expose 7860
- [ ] `docker-compose.yml`: qwe-qwe сервис + volumes для persistence (db, memory, logs)
- [ ] ENV-переменные для LLM_BASE_URL (указать на host: `host.docker.internal:1234`)
- [ ] Документация: "Запуск через Docker" секция в README
- [ ] `.dockerignore`: .git, .venv, __pycache__, logs/, *.db

### 3.2 One-line install улучшение
**Описание**: Сделать install.sh умнее — автоопределение ОС, GPU, пакетного менеджера.
**Файлы**: `install.sh`
**Задачи**:
- [ ] Определение ОС: macOS / Ubuntu / Fedora / Arch / WSL
- [ ] Проверка Python 3.11+ с подсказкой установки для каждой ОС
- [ ] Автоопределение LM Studio / Ollama после установки
- [ ] Цветной вывод с прогрессом

---

## Фаза 4 — Новые возможности

### 4.1 Мультимодальность (vision)
**Описание**: Поддержка изображений в чате — Qwen 3.5 9B поддерживает vision.
**Файлы**: `agent.py`, `server.py`, `static/index.html`, `cli.py`, `telegram_bot.py`
**Задачи**:
- [ ] agent.py: поддержка `content: [{type: "image_url", ...}, {type: "text", ...}]` в messages
- [ ] server.py: endpoint `POST /api/upload` для загрузки изображений, base64 encoding
- [ ] Web UI: drag-and-drop / paste изображений в чат, превью
- [ ] CLI: команда `/image path/to/file.png` или автоопределение пути к изображению
- [ ] Telegram: автоматическое получение фото из сообщений, пересылка в агент
- [ ] Определение поддержки vision у текущей модели (не все модели поддерживают)
- [ ] Сохранение изображений: `uploads/` директория, ссылки в истории

### 4.2 RAG по локальным файлам
**Описание**: Индексация и поиск по локальным документам пользователя (txt, md, pdf, code).
**Файлы**: новый `rag.py`, `tools.py`, `memory.py`
**Задачи**:
- [ ] `rag.py`: модуль индексации файлов
  - Поддержка форматов: .txt, .md, .py, .js, .pdf (через pypdf)
  - Chunking: разбиение на фрагменты по 512 токенов с overlap 64
  - Embedding через тот же nomic-embed-text
  - Хранение в отдельной Qdrant коллекции `qwe_rag`
- [ ] Новые tools:
  - `rag_index(path)` — проиндексировать файл или директорию
  - `rag_search(query)` — поиск по проиндексированным документам
  - `rag_status()` — статус индекса (файлов, чанков, размер)
- [ ] CLI команда: `/index ~/Documents/notes`
- [ ] Web UI: страница управления индексом
- [ ] Инкрементальная индексация: отслеживание mtime файлов, переиндексация при изменениях
- [ ] Опциональная зависимость: pypdf не в базовых requirements

### 4.3 Кэширование скиллов
**Описание**: Сейчас скиллы re-import-ятся при каждом turn. Добавить кэширование.
**Файлы**: `skills/__init__.py`
**Задачи**:
- [ ] Module cache: загружать скилл один раз, перезагружать только при изменении файла (mtime)
- [ ] Очистка стейла: если скилл удалён из диска — убрать из active_skills
- [ ] JSON вместо CSV для `active_skills` в DB (для будущих метаданных)

---

## Фаза 5 — Структура проекта и тесты

### 5.1 Реструктуризация в пакет
**Описание**: Перенести модули из корня в пакет `qwe_qwe/` для чистоты.
**Файлы**: все `.py` модули
**Задачи**:
- [ ] Создать `qwe_qwe/` пакет с `__init__.py`
- [ ] Переместить: agent, config, db, memory, soul, tools, tasks, scheduler, logger, providers, threads, vault, telegram_bot, server → `qwe_qwe/`
- [ ] `cli.py` → `qwe_qwe/__main__.py` + корневой `cli.py` как entrypoint
- [ ] Обновить все imports
- [ ] Обновить `pyproject.toml` packages
- [ ] Обновить install.sh / setup.sh

### 5.2 Тесты
**Описание**: Базовое покрытие тестами критических путей.
**Файлы**: новая директория `tests/`
**Задачи**:
- [ ] `tests/test_json_repair.py` — JSON repair функция (важно для надёжности)
- [ ] `tests/test_tools.py` — safety blockers в shell, парсинг аргументов
- [ ] `tests/test_soul.py` — генерация промпта, трейты
- [ ] `tests/test_scheduler.py` — парсинг расписаний ("in 5m", "daily 09:00")
- [ ] `tests/test_config.py` — env-переменные, defaults
- [ ] `tests/test_skills.py` — загрузка, enable/disable
- [ ] CI: GitHub Actions — pytest на push/PR
- [ ] Цель: покрытие критических функций, НЕ 100% coverage

---

## Фаза 6 — Web UI и UX

### 6.1 Rate limiting и базовая аутентификация
**Описание**: Защита web-интерфейса при доступе из LAN.
**Файлы**: `server.py`
**Задачи**:
- [ ] Опциональный пароль для web UI (задаётся в setup или env)
- [ ] Rate limit: максимум 10 запросов/мин на WebSocket (защита от flood)
- [ ] Заголовок X-Forwarded-For учёт при работе за reverse proxy

### 6.2 Улучшение Web UI
**Описание**: Качество жизни в интерфейсе.
**Файлы**: `static/index.html`
**Задачи**:
- [ ] Индикатор подключения к LLM серверу (зелёный/красный)
- [ ] Показ текущей модели и провайдера в header
- [ ] Счётчик токенов за сессию
- [ ] Export чата (markdown/json)
- [ ] Keyboard shortcuts: Enter — отправить, Shift+Enter — новая строка
- [ ] Mobile-responsive layout

---

## Порядок выполнения

```
Фаза 1 (1-2 недели)    ██████████  Фундамент — без этого остальное ненадёжно
  1.2 JSON Repair         ← ПЕРВОЕ: критично для Qwen 3.5 9B
  1.1 Безопасность        ← быстро, важно
  1.5 Config через env    ← убирает хардкод
  1.3 Сжатие промпта      ← экономит контекст
  1.4 requirements.txt    ← 5 минут

Фаза 2 (1 неделя)      ████████    Онбординг — первое впечатление
  2.1 Auto-discovery
  2.2 Web Onboarding

Фаза 3 (1 неделя)      ██████      Docker — простота запуска
  3.1 Docker
  3.2 Install улучшение

Фаза 4 (2-3 недели)    ████████████ Фичи — ценность для пользователей
  4.1 Мультимодальность   ← WOW-фактор
  4.3 Кэширование скиллов ← быстрый fix
  4.2 RAG                 ← сложнее, но высокая ценность

Фаза 5 (1 неделя)      ██████      Качество кода
  5.2 Тесты               ← покрыть критичное
  5.1 Реструктуризация    ← breaking change, делать аккуратно

Фаза 6 (1 неделя)      ██████      Polish
  6.1 Rate limiting
  6.2 UI улучшения
```

## Technical Notes
- Qwen 3.5 9B: 32K контекст, поддерживает vision, tool-calling нестабилен — JSON repair критичен
- Все фичи должны работать офлайн (кроме weather skill и finance exchange rates)
- Docker: LM Studio всегда на хосте (GPU), qwe-qwe в контейнере (CPU)
- RAG: pypdf как optional dependency, не ломает базовую установку
- Реструктуризация (5.1) — breaking change для существующих установок, нужна миграция

## Open Questions
- Нужно ли поддерживать Windows нативно или достаточно WSL + Docker?
- Стоит ли добавить поддержку GGUF моделей напрямую (без LM Studio)?
- RAG: какой максимальный размер индекса разумен для 8GB RAM машины?
