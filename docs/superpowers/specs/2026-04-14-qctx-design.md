# qctx — MCP-плагин knowledge graph для Claude Code

**Статус:** Design Proposal
**Дата:** 2026-04-14
**Scope:** v1

---

## 1. Motivation

У пользователя ~97 локальных репозиториев в `~/Documents/GitHub/`. Claude Code плохо ориентируется между ними: помнит только текущий проект, не видит связей с остальными. Прямая загрузка всего кода в контекст невозможна (объём) и не нужна — нужны **мета-знания** о проектах: назначение, стек, структура, связи, принятые архитектурные решения.

Уже развёрнутый Qdrant (`https://engine-qdrant.jyohlh.easypanel.host/`) используем как backend векторной БД + графа знаний.

## 2. Goals

- Один MCP-плагин, подключаемый к Claude Code через stdio
- Индексирует 97 репо с минимумом времени до первой пользы (структурный граф за минуты)
- LLM-обогащение поверх — карточки проектов, модулей, файлов кода
- Документация (README/docs/CHANGELOG) чанкуется и индексируется как есть
- Hybrid search (dense + sparse + BM25) из коробки
- Инкрементальное обновление по git-diff, без full-reindex
- Claude Code может дописывать заметки в граф (`remember`) — граф растёт органически в процессе работы
- Гибкий LLM-провайдер: Claude Haiku по умолчанию, любой OpenAI-совместимый endpoint через env, per-repo override для приватных проектов

## 3. Non-Goals (v1)

- Wiki-синтез (ночной batch) — отложен в v2
- Multi-user / auth — single-user инструмент
- GUI / web UI для графа — достаточно CLI и Claude Code
- FS watcher демон — покрывается git hook'ом
- Автоматическая классификация entity_type через LLM
- Индексация непо-гит-овых директорий

## 4. Architecture Overview

Rust workspace `qctx` с тремя крейтами:

```
qctx/
├── crates/
│   ├── qctx-core/   # вся логика (library crate)
│   │   ├── walker.rs, skeleton.rs, chunker.rs
│   │   ├── embed.rs, llm.rs, qdrant.rs
│   │   ├── graph.rs, model.rs
│   ├── qctx-cli/    # bulk indexer (binary)
│   └── qctx-mcp/    # MCP server on rmcp (binary)
```

**Потоки данных:**

```
bulk:       ~/Documents/GitHub/* ──► qctx-cli ──► qctx-core ──► Qdrant
claude code:                         ┌─► qctx-mcp ──► qctx-core ──► Qdrant
                                     │
                 Claude Code ──stdio─┘
```

**Ключевые зависимости:**
- `rmcp` — MCP server (stdio)
- `qdrant-client` — официальный Rust client, named vectors для hybrid
- `tree-sitter` + грамматики (rust, python, ts/tsx, js, go, java, c/cpp, ruby, php, bash, yaml)
- `fastembed` — ONNX embeddings локально (MiniLM-L12-v2, dense 384d float16 + SPLADE++ sparse)
- `reqwest` — HTTP для LLM API (OpenAI-compatible)
- `ignore` — gitignore-aware walker
- `tokio`, `serde`, `clap`, `tracing`, `indicatif`

**Подход 2 выбран из 3 альтернатив** (подход 1 = всё в MCP-процессе, подход 3 = +FS watcher демон). Причина: bulk-индексация 97 репо — часы, она должна бежать в терминале с прогрессом, а не в MCP-сессии. Переиспользование `qctx-core` общего crate даёт околонулевое дублирование между CLI и MCP.

## 5. Data Model

**Одна коллекция Qdrant `qctx_knowledge`**. Типы точек (`kind`):

| kind | описание | текст для embedding |
|---|---|---|
| `project_card` | LLM-карточка проекта | purpose + stack |
| `module_card` | LLM-карточка модуля (top-level папка) | purpose модуля |
| `file_card` | LLM-карточка файла кода | public_api + doc summary |
| `code_skeleton` | структурные факты файла (без LLM) | сводная строка |
| `doc_chunk` | чанк README/docs/CHANGELOG | сам текст |
| `entity` | узел графа (tech/lib/pattern/concept) | name + description |
| `wiki` | синтезированная страница (v2) | полный текст |
| `note` | ручная заметка от `remember()` | сам текст |

**Общий payload-скелет:**
```json
{
  "id": "<UUIDv5, детерминированный>",
  "kind": "file_card",
  "repo": "DI-frontend",
  "repo_path": "/Users/.../DI-frontend",
  "text": "<эмбеддимое>",
  "created_ts": 0.0,
  "updated_ts": 0.0,
  "source_commit": "abc123"
}
```

**Специфичные поля:**
- `file_card`: `file_path`, `language`, `parent_module`, `parent_project`, `public_api: [string]`, `depends_on: [entity_id]`
- `code_skeleton`: `file_path`, `language`, `imports: [string]`, `exports: [string]`, `loc`
- `module_card`: `module_path`, `parent_project`, `contains_files: [id]`
- `project_card`: `name`, `stack: [string]`, `purpose`, `entry_points: [string]`, `related_repos: [id]`
- `doc_chunk`: `file_path`, `chunk_index`, `chunk_total`
- `entity`: `name`, `entity_type`, `relations: [{to, rel, weight}]`, `mentioned_by: [id]`
- `note`: `scope` (raw string), `scope_kind` ("project"/"module"/"file"/"entity"/"global"), `scope_repo?`, `scope_path?`, `scope_entity?`, `author` ("claude-code"/"user"), `tags: [string]`

**ID = UUIDv5(namespace="qctx", name=`"{kind}:{repo}:{path}:{extra}"`)** — детерминированный, повторная индексация = upsert в ту же точку.

**Граф:** ссылки — строки ID, не вложенные объекты. Двунаправленные связи поддерживаются вручную (`file.depends_on ↔ entity.mentioned_by`, `entity.relations ↔ обратные relations`). Фиксированный enum типов связей: `uses`, `implements`, `depends_on`, `part_of`, `used_with`, `similar_to`, `replaces`.

**Qdrant payload indexes:**
- keyword: `kind`, `repo`, `language`, `entity_type`, `parent_project`, `parent_module`, `scope_kind`, `scope_repo`, `author`
- keyword array: `stack`, `tags`, `depends_on`, `mentioned_by`, `aliases`
- float: `updated_ts`, `tombstoned_at`
- text (full-text): `text` — word tokenizer, lowercase

**Vectors config:**
- `dense`: 384d, `Distance::Cosine`, `Datatype::Float16` (storage quantization — MiniLM выдаёт float32, в Qdrant складывается как float16; накладные на recall пренебрежимо малы, экономия памяти ×2)
- `sparse`: SPLADE++ с `modifier=idf` (IDF-вес редких токенов, даёт корректный BM25-like ranking поверх learned-sparse)

Одна коллекция — потому что cross-kind запросы естественны ("найди всё про auth в repo X"), payload-filter по `kind` быстрее чем JOIN между коллекциями.

## 6. Indexing Pipeline

`qctx index <paths...>` — 6 фаз, каждая чекпоинтится и может быть продолжена через `--resume`.

### Фаза 0: Discovery

`ignore::WalkBuilder` с уважением gitignore. Конфиг в `~/.config/qctx/config.toml`:

```toml
[index]
code_ext = ["rs","py","ts","tsx","js","jsx","go","java","rb","php","c","cpp","h"]
doc_ext  = ["md","mdx","rst","txt","adoc"]
manifest = ["Cargo.toml","package.json","pyproject.toml","go.mod","requirements.txt","Gemfile","pom.xml"]
exclude_dirs = ["node_modules","dist","build","target","__pycache__",".venv"]
max_file_size_kb = 512
```

Манифест-файл каждого репо парсится для извлечения стека + имени + описания (без LLM).

### Фаза 1: Skeleton (tree-sitter, быстрая)

Per-file через tree-sitter query:
```scheme
(import_statement) @import
(function_definition name: (identifier) @fn)
(class_definition name: (identifier) @cls)
```

Извлекаем: `imports`, `exports`, `top-level symbols`, `language`, `loc`.

- Upsert `code_skeleton` в Qdrant
- Создаём placeholder `module_card` с `contains_files`
- Создаём placeholder `project_card` со `stack` из манифеста

**После этой фазы граф работает.** Запрос "какие файлы импортируют react" отрабатывает.

### Фаза 2: Doc chunking

README, CHANGELOG, LICENSE, docs/*.md, ADR/*.md. Чанкер:
1. Сначала делит по Markdown-заголовкам (H1/H2/H3)
2. Если секция >800 chars — по абзацам с overlap 100
3. Минимум 200 chars на чанк

### Фаза 3: Embeddings

`fastembed-rs` батчами 64. Эмбеддятся только точки без векторов (флаг в checkpoint-state). На M-series ~2000 эмбеддингов/сек CPU.

### Фаза 4: LLM-обогащение

Приоритет: `project_card` → `module_card` → `file_card`.

Правила для `file_card`:
- Файлы >50 LOC или entry-points → LLM-карточка
- Маленькие файлы (<50 LOC) → auto-summary из скелета (без LLM)

Промпт (~1.5k input tokens) включает project_context, file skeleton и первые 250 строк кода. Ответ — strict JSON с `purpose`, `public_api`, `depends_on`, `tags`. Retry при невалидном JSON.

Параллелизм: N воркеров (конфиг). Локальный LLM — N=1, Claude Haiku — N=8. Rate-limit через токенное окно.

**Per-repo privacy override** в `.qctx.toml` в корне репо: `privacy = "local"` форсит локальную LLM для этого репо.

### Фаза 5: Entity-граф

После каждой file_card:
1. Нормализация имен в `depends_on`: `casefold` + strip версии/скобки + резолв через alias-таблицу (например `postgres == PostgreSQL == psql`)
2. Payload-фильтр `kind=entity AND name=<normalized>` — поиск существующей
3. Existing → `append file_id → entity.mentioned_by`
4. New → create с `type="tech"` (эвристика: суффиксы `-db`, whitelist, иначе "concept")

**Alias-таблица** — 3-уровневая, резолв идёт в порядке приоритета:

1. **Builtin** (seed, зашит в бинарник) — `qctx-core/src/aliases.toml`, ~300 пар: популярные технологии, их ребрендинги, общепринятые сокращения. Обновляется с релизами. Примеры: `postgres → postgresql`, `nodejs → node`, `ts → typescript`, `k8s → kubernetes`.
2. **User config** (опциональный) — `~/.config/qctx/aliases.toml`, override/extension builtin. Редактируется вручную.
3. **Runtime в Qdrant** (автовычисляемый) — при `qctx entity link A --alias-of B` или через CLI. Хранится как payload-поле `aliases: [...]` у самого entity-ноды. Поиск при резолве идёт в две стороны: по `name` и по `aliases`.

Автоматическая дедупликация entities cross-repo. Ручной retype через `qctx entity retype <name> <new_type>`. Ручное связывание алиаса через `qctx entity link <alias> --alias-of <canonical>`.

### Фаза 6: Прогресс и checkpointing

Многоуровневый `indicatif` progress bar. Чекпоинт `~/.cache/qctx/state.json` — каждые 100 операций. `--resume` продолжает с последнего чекпоинта. `--skip-llm` ограничивает запуск фазами 0–3.

**Таймлайн для 97 репо (M-class, Claude Haiku):**
- Фазы 0–3 (скелет + docs + embeddings): **10–20 минут**
- Фаза 4 (LLM-карточки на ~5k файлов): **40–60 минут** при 8 воркерах, ~$10–20

## 7. MCP Tools Surface

Транспорт — stdio. Регистрация в `~/.claude/mcp_servers.json`:

```json
{
  "mcpServers": {
    "qctx": {
      "command": "qctx-mcp",
      "env": {
        "QCTX_QDRANT_URL": "https://engine-qdrant.jyohlh.easypanel.host",
        "QCTX_QDRANT_API_KEY": "…",
        "QCTX_LLM_URL": "https://api.anthropic.com/v1",
        "QCTX_LLM_MODEL": "claude-haiku-4-5-20251001"
      }
    }
  }
}
```

### 9 инструментов + health

**Retrieval:**
- `search(query, kind?, repo?, limit?=10)` → hybrid dense+sparse+text RRF
- `get_card(id)` → полный payload + дотягивает parent/depends
- `find_similar(id, limit?=5)` → Qdrant recommend API

**Graph:**
- `related(id, depth?=1, rel_types?)` → обход графа с visited-set

**Discovery:**
- `list_projects(stack?, has_tag?)` → scroll по project_card
- `list_stacks()` → aggregation по entity.mentioned_by, cached 10min

**Writing:**
- `remember(scope, text, tags?)` → создаёт note-точку, author=claude-code, эмбеддинг on-the-fly

  **Scope grammar (обязательное поле, строка одного из видов):**
  - `"project:<repo_name>"` — факт про проект целиком; payload-filter по `repo`; появляется при `list_projects` и `search(repo=X)`
  - `"module:<repo_name>:<module_path>"` — факт про конкретный модуль; в поиске всплывает при `search(repo=X)` и при `related(module_id)`
  - `"file:<repo_name>:<file_path>"` — факт про один файл; привязан к `parent_file` id
  - `"entity:<entity_name>"` — факт про технологию/концепт; появляется при `get_card(entity_id)` и `related(entity_id)`
  - `"global"` — не привязано ни к чему; доступно только через общий `search` без фильтров

  Scope парсится в поля `scope_kind`, `scope_repo`, `scope_path`, `scope_entity` для фасет-фильтрации. Невалидный scope → tool возвращает ошибку, note не создаётся.

**Admin:**
- `index_repo(path, skip_llm?=false, privacy?)` → background job, возвращает job_id
- `refresh(repo, force_llm?=false)` → git-diff-driven инкрементальный refresh
- `job_status(job_id)` → state + progress + log_tail

**Служебный:**
- `health()` → {qdrant, embedder, llm, cache_size, version}

### Что MCP НЕ делает

- Удаление проектов (`qctx drop <repo>`) — только через CLI
- Ручное связывание entity (`qctx entity link A B --rel uses`) — только через CLI
- Massive reindex всех репо (`qctx index --all`) — только через CLI

Защита от случайного вайпа.

## 8. Updates & Incrementality

**Источник правды для изменений** — `git diff --name-status <source_commit>..HEAD` + `git status --porcelain` для dirty working tree.

**Per-file handling:**

- **Modified:** re-skeleton, upsert. Считаем `signature_hash = sha256(canonical_json({imports, exports, public_symbols, first_doc_comment_line, module_docstring}))` — поля, которые семантически влияют на то, что LLM скажет про файл. LOC, whitespace, комментарии внутри тел функций НЕ входят — чтобы косметические правки не триггерили re-LLM. Если `signature_hash` изменился ИЛИ `--force-llm` → re-LLM. Иначе только skeleton обновляется. Diff `depends_on` old vs new → update entity.mentioned_by на обеих сторонах.
- **Added:** полный пайплайн.
- **Deleted:** удалить все точки с `file_path=X AND repo=R`, убрать `file_id` из всех entity.mentioned_by, обновить module_card.contains_files.

  **Orphan entities** (mentioned_by=[] после удаления последней ссылки):
  1. Не удаляем сразу — ставим payload-поле `tombstoned_at = now()` (unix ts, индексируется как float)
  2. Если в течение 24ч entity снова получает `mentioned_by` (например при индексации другого репо) — `tombstoned_at` стирается, entity "оживает"
  3. Очистка tombstone'ов — ленивая: выполняется в начале каждого `qctx refresh`, `qctx index` и `qctx doctor`. Запрос: `filter(kind=entity AND tombstoned_at < now()-86400)` → delete. Отдельный cron/демон не нужен.
  4. `qctx doctor` явно показывает tombstone count: `tombstoned entities (will be purged on next op): 3`
- **Renamed:** Delete + Add, но LLM-карточка переносится копированием payload.

**Manifest change:** re-read стек, обновить `project_card.stack`, added/removed deps → entity links.

**Инварианты (проверяются `qctx doctor`):**

- I1: Bidirectional consistency: `∀ card, entity_id ∈ card.depends_on ⇒ card_id ∈ entity.mentioned_by`
- I2: No dangling refs: `∀ entity, file_id ∈ entity.mentioned_by ⇒ точка file_id существует`

**Concurrency:**

- Глобальный файл-lock `~/.cache/qctx/lock` (`fcntl` advisory, `F_SETLK`). Защищает от одновременной записи в Qdrant.
- CLI держит lock на всё время индексации/rebuild.
- MCP `index_repo` и `refresh` при запуске пытаются `try_lock`:
  - **Не смог взять (CLI работает)** → тул сразу возвращает `{job_id: null, state: "rejected", reason: "qctx-cli is running — wait or cancel it"}`. Claude Code видит и может сообщить пользователю.
  - **Смог взять** → создаёт tokio-task, возвращает `job_id`, lock отпускается только после завершения задачи.
- Между собой MCP-тулы сериализуются через единый tokio `JoinSet` + semaphore на N воркеров (из конфига, default 2). Вторая параллельная `index_repo` попадает в очередь, не падает.
- Чтение (`search`, `get_card`, `list_*`) lock НЕ берёт — Qdrant конкурентен на чтение. Это критично чтобы Claude Code не тормозил когда CLI индексирует.

Background jobs в MCP живут только в рамках процесса MCP — если MCP рестартит, незавершённая индексация теряется; `refresh` нужно позвать заново. Персистентная очередь — v2.

**Git hook (опционально):** `qctx install-hooks <repo>` добавляет `.git/hooks/post-commit` с фоновым `qctx refresh --quiet`. По умолчанию выключено.

**Full re-index:**
- `qctx rebuild --vectors` — пере-эмбеддинг без tree-sitter/LLM (после смены модели)
- `qctx rebuild --schema` — rewrite payload формата
- `qctx doctor --repair` — fix graph drift
- `qctx drop && qctx index --all` — с нуля

## 9. Testing & Observability

### Unit-тесты (per-crate)

- `walker`: .gitignore, exclude_dirs, max_file_size
- `skeleton`: snapshot-тесты через `insta` для каждого языка, edge cases (empty, invalid syntax, unicode)
- `chunker`: markdown-split, fallback на size-based, overlap
- `model`: UUIDv5 детерминированность, round-trip сериализации
- `llm`: JSON парсинг с retry, mock HTTP через `wiremock`
- `graph`: link/unlink обновляют обе стороны, normalize_entity_name

### Интеграционные тесты

- `qdrant`: testcontainers с `qdrant/qdrant:latest`, end-to-end на минимальном fixture-repo
- `qctx-cli`: smoke-тест индексации, `--resume` после прерывания (kill)
- `qctx-mcp`: stdio JSON-RPC клиент, `tools/list` + `tools/call` на фикстуре

### Property-based (quickcheck)

- После произвольной последовательности `index / refresh / delete` — I1 и I2 сохраняются
- Повторный `index` без изменений — 0 новых точек, 0 обновлений payload (идемпотентность)

### Manual verification checklist

В `docs/VERIFICATION.md` чеклист перед релизом v1:
- Index одного репо со `--skip-llm` и с LLM
- Claude Code session с 3-4 сценариями (project overview, cross-repo поиск стека, find_similar, remember)
- Refresh после modify/delete
- Bulk 5 репо с `--resume` после kill

### Observability

- `tracing` crate: info/debug/warn/error. Stderr + rotating log `~/.cache/qctx/logs/qctx.log`
- `QCTX_LOG=debug` для verbose
- `qctx stats` — сводка по коллекции, coverage, top entities
- MCP `health` тул для "alive check" при старте сессии
- OpenTelemetry метрики под флагом (опционально): duration/tokens/upserts

## 10. Configuration

**Environment variables (MCP + CLI):**
- `QCTX_QDRANT_URL` (required)
- `QCTX_QDRANT_API_KEY` (если сервер требует)
- `QCTX_LLM_URL` (default: `https://api.anthropic.com/v1`)
- `QCTX_LLM_API_KEY`
- `QCTX_LLM_MODEL` (default: `claude-haiku-4-5-20251001`)
- `QCTX_CONFIG` (путь к TOML-конфигу, default `~/.config/qctx/config.toml`)
- `QCTX_LOG` (trace/debug/info/warn/error, default `info`)

**TOML config:** `~/.config/qctx/config.toml` — расширенные опции (extensions, excludes, concurrency limits, rate limits).

**Per-repo config:** `.qctx.toml` в корне репо, override отдельных полей (privacy, exclude, extra manifest paths).

## 11. Out of Scope (v1)

- Wiki-синтез (v2)
- Multi-user / auth MCP сервера
- Web UI для графа
- FS watcher демон (Подход 3)
- Автоклассификация entity_type через LLM
- Индексация non-git директорий
- Visual graph explorer (Mermaid/Graphviz export — easy add later)

## 12. Open Questions

- Какое имя проекта финальное? (`qctx` рабочее)
- Где хостим/распространяем бинари? (homebrew tap, cargo install, github release?)
- Как обновлять грамматики tree-sitter — через `cargo update` или vendored submodules? (определится при имплементации)
- Нужна ли интеграция с `.gitignore`-like файлом `.qctxignore` отдельно? (v1 — нет, можно через exclude_dirs в config)

## 13. Future (v2+)

- Wiki-синтез ночным batch'ем (как в qwe-qwe synthesis.py)
- FS watcher демон
- GUI / веб-интерфейс для навигации графа
- Graph export (Mermaid, Graphviz, JSON-LD)
- Автоматическая классификация entity_type
- Enterprise: multi-user, role-based access
