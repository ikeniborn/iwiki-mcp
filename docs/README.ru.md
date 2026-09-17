# iwiki-mcp

*English version: [../README.md](../README.md).*

## Что это

iwiki-mcp — общая wiki-служба с доменами и MCP-доступом из Codex и Claude Code.

Поддерживаемый контейнер запускает hosted iwiki MCP, nginx и
[Telegram-бот](telegram-bot.md) вместе. Сотрудники из allowlist могут выбирать домены,
задавать текстовые или голосовые вопросы и подтверждать изменения страниц; ввод `/`
показывает список команд бота, а `/menu` открывает inline-меню действий. Выбранный
домен сохраняется на всё время работы процесса бота, вопрос, отправленный до выбора
домена, будет отвечен сразу после выбора, а ход обработки каждого запроса показывается
реакцией, статусом «печатает» и статус-сообщением, которое редактируется по этапам. При
провайдере инференса с поддержкой tool calling бот отвечает через агентный цикл
поиска и чтения по wiki, автоматически откатываясь на однопроходное извлечение
контекста, если провайдер не поддерживает инструменты.
Точный
операторский путь и миграция описаны в [deployment runbook](deployment.md).
Поддерживаются локальная Git-синхронизируемая база или tenant-isolated PostgreSQL,
через stdio или hosted Streamable HTTP — см.
[Режимы хранения и транспорта](storage-modes.ru.md).

## Установка

Требуется Python `>=3.10`. Рекомендуемый инструмент — [`uv`](https://docs.astral.sh/uv/); `pipx` подходит как полная замена.

### Глобальный инструмент (рекомендуется для работы)

iwiki-mcp **ещё не опубликован на PyPI**, поэтому ставьте из локальной копии. Клонируйте репозиторий и выполните из его корня:

```bash
git clone https://github.com/ikeniborn/iwiki-mcp.git
cd iwiki-mcp
uv tool install .
# или
pipx install .
```

Это помещает исполняемый файл `iwiki-mcp` в `PATH` (например, `~/.local/bin/iwiki-mcp`) — именно его запускает MCP-клиент. Проверьте через `iwiki-mcp --help`.

После публикации пакета глобальная установка станет однострочной — `uv tool install iwiki-mcp` (или `pipx install iwiki-mcp`). До тех пор эти команды падают с `No matching distribution found for iwiki-mcp`; используйте установку из локальной копии выше.

### Из исходников (разработка)

Клонируйте, синхронизируйте зависимости (включая extra `dev`) и прогоните тесты:

```bash
git clone https://github.com/ikeniborn/iwiki-mcp.git
cd iwiki-mcp
uv sync --extra dev
uv run pytest -q
```

После этого `uv run iwiki-mcp` запускает сервер из копии без глобальной установки.

## Требования

iwiki-mcp требует OpenAI-совместимый endpoint эмбеддингов. Задайте `IWIKI_LLM_BASE_URL` и `IWIKI_LLM_KEY` в окружении MCP-клиента (см. [Регистрация в Claude Code](#регистрация-в-claude-code) / [Регистрация в Codex](#регистрация-в-codex)).

MCP-клиент запускает `iwiki-mcp` по stdio в начале сессии. Это не демон; сервер живёт в течение сессии клиента. Перед открытием MCP stdio обычный запуск отправляет один минимальный запрос в настроенный endpoint эмбеддингов с таймаутом 10 секунд и без повторных попыток. Отсутствующая или некорректная конфигурация, недоступный endpoint либо некорректный ответ блокируют запуск и выводят в stderr диагностическое сообщение с указанием дальнейших действий; буквальное значение настроенного API-ключа в диагностических полях заменяется на `<redacted>`. `iwiki-mcp --help` остаётся офлайн и не выполняет проверочный запрос.

## Регистрация в Claude Code

Пошагово:

1. **Проверьте, что исполняемый файл доступен.** `iwiki-mcp --help` должен вывести справку. Если нет — глобальная установка не попала в `PATH`: переустановите (`uv tool install .`) или используйте `uv run iwiki-mcp` как команду.
2. **Зарегистрируйте сервер.** Либо запустите CLI из корня проекта:

   ```bash
   claude mcp add iwiki \
     --env IWIKI_LLM_BASE_URL=https://.../v1 \
     --env IWIKI_LLM_KEY=... \
     --env IWIKI_BASE_DIR=/home/user/wiki \
     -- iwiki-mcp
   ```

   либо добавьте тот же блок в `.mcp.json` в корне проекта вручную:

   ```json
   {
     "mcpServers": {
       "iwiki": {
         "command": "iwiki-mcp",
         "env": {
           "IWIKI_LLM_BASE_URL": "https://.../v1",
           "IWIKI_LLM_KEY": "...",
           "IWIKI_BASE_DIR": "/home/user/wiki"
         }
       }
     }
   }
   ```

3. **Проверьте.** Выполните `claude mcp list` — `iwiki` должен показываться как connected. Внутри сессии `/mcp` перечисляет инструменты `wiki_*`.
4. **Не храните секреты в git.** Поместите `IWIKI_LLM_KEY` (и обычно `IWIKI_LLM_BASE_URL`) в пользовательский или `.local` конфиг, а не в коммитимый `.mcp.json`.

Клиент запускает сервер с `cwd` в корне проекта, поэтому `.iwiki.toml` (см. [Привязка проекта](wiki-model.ru.md#привязка-проекта)) подхватывается автоматически.

## Регистрация в Codex

Пошагово:

1. **Проверьте доступность исполняемого файла:** `iwiki-mcp --help`.
2. **Добавьте сервер** в `~/.codex/config.toml`:

   ```toml
   [mcp_servers.iwiki]
   command = "iwiki-mcp"
   env = { IWIKI_LLM_BASE_URL = "https://.../v1", IWIKI_LLM_KEY = "...", IWIKI_BASE_DIR = "/home/user/wiki" }
   ```

   Чтобы запускать из исходной копии вместо глобальной установки, используйте `command = "uv"` с `args = ["run", "iwiki-mcp", "--project", "/abs/path/to/project"]`.
3. **Перезапустите Codex**, чтобы он перечитал `config.toml`, затем начните сессию в проекте. Инструменты `wiki_*` станут доступны.

Codex не устанавливает `cwd` сервера в ваш проект, поэтому передавайте `iwiki-mcp --project /abs/path/to/project` (или задайте `IWIKI_PROJECT_DIR` в `env`), когда корень проекта отличается от места запуска Codex — именно так разрешается `.iwiki.toml`.

## Быстрый старт

1. Установите `iwiki-mcp` и зарегистрируйте его в Claude Code или Codex с `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY` и `IWIKI_BASE_DIR`.
2. В сессии агента создайте домен:

```text
wiki_create_domain(name="backend")
```

3. Вручную отредактируйте созданный `.iwiki.toml` (см. [Привязка проекта](wiki-model.ru.md#привязка-проекта)), затем добавьте сниппет для агента (см. [Научите агента пользоваться iwiki](#научите-агента-пользоваться-iwiki)):

```toml
read = ["backend"]
write = ["backend"]
primary = "backend"
```

4. Запишите первую страницу:

```text
wiki_write_page(
  domain="backend",
  slug="auth",
  markdown="# Auth\n\n## Purpose\nAuth verifies users and protects private routes.\n",
  description="Token authentication flow.",
  type="architecture"
)
```

Это создаёт `backend/architecture/auth.md`; передавайте ту же идентичность `architecture/auth` как `slug` в `wiki_read_page` / `wiki_update_page` / `wiki_delete_page`.

5. Найдите её:

```text
wiki_search(query="how does auth work?")
```

## Научите агента пользоваться iwiki

Регистрация сервера открывает инструменты, но агенту всё ещё нужны указания, *когда* их вызывать. В репозитории есть готовые сниппеты в [`templates/`](../templates):

- `templates/CLAUDE.md.snippet` — добавьте в `CLAUDE.md` проекта (Claude Code).
- `templates/AGENTS.md.snippet` — добавьте в `AGENTS.md` проекта (Codex).

Оба несут одинаковые указания: искать перед задачей, не менять привязку при обычном старте проекта, писать страницы после изменений функциональности и вызывать `wiki_sync` в конце сессии. Добавьте нужный сниппет один раз на проект:

```bash
cat templates/CLAUDE.md.snippet >> CLAUDE.md   # Claude Code
cat templates/AGENTS.md.snippet >> AGENTS.md   # Codex
```

Сниппеты ссылаются на `.iwiki.toml`, поэтому сначала [привяжите проект](wiki-model.ru.md#привязка-проекта).

## Документация

Всё, что выходит за рамки установки и регистрации, вынесено в `docs/`. У каждой
страницы есть английский оригинал с тем же именем без суффикса `.ru`.

| Страница | О чём |
|---|---|
| [Режимы хранения и транспорта](storage-modes.ru.md) | Git stdio, PostgreSQL stdio, hosted Streamable HTTP, поддерживаемый контейнер, время жизни сессии и контракт MCP-инструментов PostgreSQL. |
| [Подготовка PostgreSQL](postgres-setup.ru.md) | Роли с минимальными привилегиями, админский CLI, резервные копии и восстановление, миграции v4/v5 и откат. |
| [База вики, домены и привязка](wiki-model.ru.md) | Структура базы, графовый кэш и ссылки, привязка через `.iwiki.toml`, Git-синхронизация базы. |
| [Python code graph](code-graph.ru.md) | Опциональный локальный code graph: конфигурация, задачи сборки, инструменты чтения и покрытие по языкам. |
| [Публикация code graph](code-graph-publishing.ru.md) | Режимы распределённой публикации, CLI публикатора, планирование и профили снапшота SQLite. |
| [Given-When-Then спецификации](specifications.ru.md) | Режимы спецификаций, грамматика сценариев, семантические инструменты и находки lint. |
| [Инструменты](tools-reference.ru.md) | Каждый инструмент `wiki_*` и его контракт. |
| [Переменные окружения](env-reference.ru.md) | Каждая переменная `IWIKI_*` и её значение по умолчанию. |
| [Совместимость с OKF](okf-compatibility.ru.md) | Поля frontmatter, зарезервированные файлы и инструменты перехода на OKF. |
| [Бенчмарки](benchmarks.ru.md) | Прогоны оценки code graph, search pipeline и Pareto. |
| [Архитектура](architecture.md) | Внутренняя карта модулей и поток данных (только EN). |
| [Deployment runbook](deployment.md) | Операторский путь: развёртывание контейнера, миграция, cutover, откат (только EN). |
| [Telegram-бот](telegram-bot.md) | Встроенный сервис Telegram-бота (только EN). |

## Ограничения (v1)

- Внутри домена используйте `[Heading](<type>/<slug>.md#heading)`; между доменами — `iwiki://<domain>/<page-id>#<anchor>`.
- `.iwiki/graph.sqlite3` — локальный производный кэш, а не переносимая замена векторам/логам и не граф code-dependencies.
- Git storage использует numpy brute-force поиск по переносимым JSONL-индексам;
  PostgreSQL storage получает tenant/domain-scoped cosine-кандидатов через pgvector,
  затем применяет общие lexical fusion, deduplication и опциональный reranking.
- Проверки устаревания локальны для проекта и зависят от доступных путей к исходникам и логов ingest.
