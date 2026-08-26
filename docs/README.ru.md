# iwiki-mcp

*English version: [../README.md](../README.md).*

## Что это

iwiki-mcp — общая wiki-служба с доменами и MCP-доступом из Codex и Claude Code.

Отдельно разворачиваемый [Telegram-бот](telegram-bot.md) позволяет сотрудникам из
allowlist выбирать домены, задавать текстовые или голосовые вопросы и подтверждать
изменения страниц через hosted iwiki.
Поддерживаются локальная Git-синхронизируемая база или tenant-isolated PostgreSQL,
через stdio или hosted Streamable HTTP по матрице ниже.

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

## Режимы хранения и транспорта

| Хранилище | stdio | Streamable HTTP |
| --- | --- | --- |
| Git-каталог | поддерживается; по умолчанию | не поддерживается |
| PostgreSQL | поддерживается для одной локально настроенной wiki | поддерживается для hosted-доступа к нескольким wiki |

### Локальный Git stdio

Существующий локальный режим не изменился:

```bash
export IWIKI_BASE_DIR=/srv/iwiki-base
iwiki-mcp --project /srv/project
```

### Локальный PostgreSQL stdio

Создайте `/srv/project/.iwiki.toml` с явной максимальной областью доменов. В отличие
от Git, PostgreSQL требует непустые массивы `read` и `write` и домен `primary`.
Указанные wiki и домены должны быть заранее созданы администратором.

```toml
read = ["backend", "frontend"]
write = ["backend"]
primary = "backend"

[storage]
type = "postgres"
host = "db.internal.example"
port = 5432
database = "iwiki"
user = "iwiki_app"
sslmode = "verify-full"
iwiki_id = "team-wiki"
```

Секреты и идентификатор модели передавайте только через окружение процесса:

```bash
export IWIKI_DB_PASSWORD='<database-password>'
export IWIKI_LLM_BASE_URL='https://models.internal.example/v1'
export IWIKI_LLM_KEY='<model-api-key>'
export IWIKI_EMBED_MODEL='lemonade-embeddings-bge-m3-q8'
export IWIKI_EMBED_DIMENSIONS='1024'
export IWIKI_RERANK_MODEL='lemonade-reranker-bge-reranker-v2-m3'
iwiki-mcp --project /srv/project
```

`wiki_bind` может сузить максимальную область текущего процесса, но не расширить её.
Для update/delete в PostgreSQL обязателен `expected_revision` из `wiki_read_page`.

### Hosted Streamable HTTP

Hosted-режим требует PostgreSQL и отдельный server TOML. Поле `iwiki_id` запрещено:
wiki и её максимальные read/write grants определяются bearer-токеном.

```toml
[storage]
type = "postgres"
host = "db.internal.example"
port = 5432
database = "iwiki"
user = "iwiki_app"
sslmode = "verify-full"

[server]
host = "127.0.0.1"
port = 8765
allowed_origins = ["https://iwiki.example"]
pool_min_size = 2
pool_max_size = 10
statement_timeout_ms = 30000
lock_timeout_ms = 5000
```

```bash
export IWIKI_SERVER_CONFIG=/etc/iwiki/server.toml
export IWIKI_DB_PASSWORD='<database-password>'
export IWIKI_LLM_BASE_URL='https://models.internal.example/v1'
export IWIKI_LLM_KEY='<model-api-key>'
export IWIKI_EMBED_MODEL='lemonade-embeddings-bge-m3-q8'
export IWIKI_EMBED_DIMENSIONS='1024'
export IWIKI_RERANK_MODEL='lemonade-reranker-bge-reranker-v2-m3'
iwiki-mcp serve --transport streamable-http
```

MCP endpoint — `/mcp`. Размещайте loopback-listener за reverse proxy: он завершает
публичный TLS, передаёт точный `Origin` и не пишет `Authorization` в логи. Для
браузера `Origin` обязан совпасть с `allowed_origins`; клиенты без `Origin` допустимы,
но каждый MCP-запрос всё равно требует `Authorization: Bearer <token>`. Ошибки
credentials, grants, session и storage возвращаются очищенными 401/403/404/503.
Hosted-режим не отправляет server-initiated notifications: после Bearer-аутентификации
`GET /mcp` возвращает `405 Method Not Allowed` с `Allow: POST, DELETE`, не входя в MCP
session manager. Stateful-запросы `POST` и завершение сессии через `DELETE` остаются
доступны.

Сервер открывает ограниченный connection pool и применяет заданные statement/lock
timeouts. До открытия listener startup проверяет модель, сверяет её метаданные и
транзакционно запускает forward-only миграции под lock. Повторный startup идемпотентен.
Одна БД хранит несколько изолированных wiki с разными `iwiki_id`. Модель эмбеддингов
и размерность — общие метаданные БД: несовпадение останавливает startup; их смена —
операторская миграция, не автоматический re-embedding. Embedding/rerank credentials
остаются только на сервере.

Каждый запрос заново читает текущие права токена. Сессия хранит явно выбранный
`selected` scope отдельно от пересечённого со свежими grants `effective` scope:
revocation действует на следующем запросе, восстановленное право возвращается только
если домен оставался selected, а новый grant целевого токена не расширяет существующую
сессию. Только успешный `wiki_create_domain` расширяет текущую сессию creator-токена.
Локальные `.iwiki.toml` и `.iwikiignore` по-прежнему создаёт и меняет инициализация
проекта; hosted-сервер создаёт состояние домена в PostgreSQL и эти файлы не пишет.

### Подготовка PostgreSQL и минимальные привилегии

Оператор создаёт БД и устанавливает расширение `vector`. Приложение создаёт и
мигрирует только схему `iwiki`. Предпочтителен отдельный login-role с `CONNECT` к БД
и владением схемой `iwiki`; другой вариант — privileged-role выполняет миграции,
после чего app-role получает только `USAGE` и нужные права на таблицы и sequences.
Не выдавайте доступ к посторонним схемам. Вне изолированного dev-хоста используйте
`sslmode="verify-full"`, доверенный CA и совпадающее имя хоста БД.

Все PostgreSQL admin-команды принимают `--config PATH`; без него читают
`IWIKI_SERVER_CONFIG`. Только bare stdio-команда принимает `--project`; `serve`
принимает только `--transport streamable-http`. `--read-domain` и `--write-domain`
можно повторять. `base show`, `base list`, `token list`, import и export поддерживают
machine-readable `--json`; только import/export поддерживают `--dry-run`.

```bash
iwiki-mcp base create --iwiki team-wiki
iwiki-mcp base list
iwiki-mcp base show --iwiki team-wiki
iwiki-mcp base disable --iwiki team-wiki
iwiki-mcp base enable --iwiki team-wiki
iwiki-mcp domain create --iwiki team-wiki --domain backend
iwiki-mcp token create --iwiki team-wiki --owner deploy --read-domain backend --write-domain backend
iwiki-mcp token create --iwiki team-wiki --owner bootstrap --can-create-domain
iwiki-mcp token list --iwiki team-wiki
iwiki-mcp token set-create-domain --iwiki team-wiki --token-id <token-id> --enabled
iwiki-mcp token set-domain-management --iwiki team-wiki --token-id <token-id> --domain backend --enabled
iwiki-mcp token revoke --token-id <token-id>
iwiki-mcp base import-git --iwiki team-wiki --path /srv/old-wiki --dry-run --json
iwiki-mcp base export-git --iwiki team-wiki --path /srv/rollback-wiki --dry-run --json
```

`token create` показывает plaintext-токен один раз; сохраните его в secret manager.
`token list` не возвращает токен и показывает `can_create_domain`, `managed_domains`,
`read_domains` и `write_domains` как в стандартном JSON, так и с `--json`.
`set-create-domain` и `set-domain-management` — server-side recovery; требуется ровно
один флаг `--enabled` или `--disabled`. Revoke токена и disable wiki действуют на
следующих запросах. Revoke токена атомарно удаляет его content/management grant rows,
но сохраняет revoked token audit record. Команды физического удаления намеренно нет.

Import читает Git wiki-репозиторий и пишет одну PostgreSQL wiki. Export требует пустой
каталог, создаёт переносимый Git-репозиторий и первый commit. `--dry-run` только
проверяет и формирует отчёт. Для локального rollback выполните export, переключите
`.iwiki.toml` проекта обратно на Git и экспортированную базу, затем запустите
`wiki_index`. Import/export не запускают `wiki_sync` автоматически.

Backup БД, шифрование, retention и учебные восстановления — ответственность оператора.
Используйте штатные PostgreSQL tools и service definition, чтобы credentials не попали
в shell history. Целевая БД для restore должна существовать заранее.

Миграция v4 — forward-only: она добавляет `can_create_domain`,
`token_domain_management_grants` и domain-leading индексы grant-таблиц; down migration
отсутствует. Старый binary отклонит schema v4, поэтому rollback binary требует restore
резервной копии до v4 либо compatibility release до запуска.

```bash
pg_dump --dbname=service=iwiki --format=custom --schema=iwiki --file=/secure/encrypted-volume/iwiki.dump
pg_restore --dbname=service=iwiki_restore --clean --if-exists --schema=iwiki /secure/encrypted-volume/iwiki.dump
```

#### Runtime-принципалы для code graph

Три роли базы данных остаются раздельными. Владелец схемы и мигратор — только
административные учётные данные: он владеет схемой `iwiki` и применяет миграции через
admin-команды, и он никогда не настраивается как логин работающего сервера. Hosted
service principal — роль, под которой подключается hosted-сервер. Direct runtime
principal — роль локального индексера в прямом режиме PostgreSQL. Обе runtime-роли не
являются владельцем, не имеют `BYPASSRLS`, не выполняют миграции и не получают `CREATE`
на базу или схему. Row-level security включается обычным
`ENABLE ROW LEVEL SECURITY`, никогда `FORCE`, поскольку владелец — административная роль.

Регистрируйте каждую runtime-роль и её доменные гранты явно. `principal grant` никогда
не создаёт роль и не принимает её пароль; создавайте логин отдельно теми учётными
данными, которыми управляет ваша платформа.

```bash
iwiki-mcp principal grant --iwiki team-wiki --principal iwiki_hosted --runtime hosted --read-domain backend --write-domain backend
iwiki-mcp principal grant --iwiki team-wiki --principal iwiki_indexer --runtime direct --read-domain backend --write-domain backend
iwiki-mcp principal inspect --principal iwiki_hosted --json
```

Подготовьте домены до включения токенов, затем выпускайте токены против точной
развёрнутой hosted-роли. `token create` требует `--hosted-principal ROLE`, где `ROLE`
равен `[storage].user` hosted-сервера. Команда проверяет, что именно эта роль
зарегистрирована как `runtime=hosted`, не является владельцем, не имеет `BYPASSRLS` и уже
покрывает каждый запрошенный домен чтения и записи, до генерации любого материала
токена. Другая hosted-роль или общая проверка «какая-то hosted-роль существует» заменой
не является.

```bash
iwiki-mcp domain create --iwiki team-wiki --domain backend
iwiki-mcp principal grant --iwiki team-wiki --principal iwiki_hosted --runtime hosted --read-domain backend --write-domain backend
iwiki-mcp token create --iwiki team-wiki --owner deploy --hosted-principal iwiki_hosted --read-domain backend --write-domain backend
iwiki-mcp serve --transport streamable-http
```

Старт выполняет одинаковую проверку схемы для hosted HTTP и stdio: сервер сверяет точную
ожидаемую версию схемы и собственный подключённый `session_user` с выданными грантами и
иначе отказывается стартовать. Миграции неявно не выполняются никогда.

#### Откат схемы v5 и артефакт совместимости

Миграция v5 добавляет таблицы code graph. Откат приложения на релиз до code graph — это
процедура обслуживания, а не развёртывание произвольного старого коммита: сырой коммит
до code graph не является поддерживаемым бинарём отката, поскольку ограниченная
runtime-роль не имеет `CREATE` на схему и такой бинарь попытается создать объекты схемы
при старте.

Поддерживаемый путь — закреплённый артефакт обслуживания
`compat/postgres-v4-runtime-guard.json` вместе с его патчем. Манифест фиксирует базовый
коммит, дайджест патча, дайджест дерева исходников и версию схемы, которую принимает
пропатченный runtime. Пересоберите и проверьте его: переключитесь на записанный базовый
коммит, примените записанный патч и подтвердите оба дайджеста до развёртывания.

```bash
iwiki-mcp schema rollback-v5-compat --json
iwiki-mcp schema rollback-v5-compat --confirm --json
```

Сухой прогон сообщает, какой маркер он удалил бы, и ничего не меняет. Только `--confirm`
удаляет маркер схемы 5, оставляя таблицы code graph на месте и неиспользуемыми. После
отката прогоните smoke пропатченного артефакта обслуживания против базы: он обязан
стартовать только на чтение под ограниченной runtime-ролью и не иметь привилегий
`CREATE` или изменения `schema_migrations`. Повторное применение миграции v5 позже —
обычная прямая миграция, она идемпотентна.

Останавливайте вывод в production, а не обходите его, когда точный hosted-принципал
недоказуем, когда отсутствует необходимый доменный грант, когда подключённый
`session_user` отличается от выданной роли, когда версия схемы не совпадает точно или
когда дайджесты артефакта обслуживания не воспроизводятся.

### Контракт MCP-инструментов PostgreSQL

| Поддержка PostgreSQL | Инструменты |
| --- | --- |
| Поддерживаются | `wiki_status`, `wiki_list_domains`, `wiki_list_pages`, `wiki_read_page`, `wiki_search`, `wiki_related`, `wiki_write_page`, `wiki_update_page`, `wiki_insert_section`, `wiki_delete_section`, `wiki_move_section`, `wiki_delete_page`, `wiki_index`, `wiki_bind`, `wiki_lint` |
| Только hosted PostgreSQL | `wiki_create_domain`, `wiki_list_domain_grants`, `wiki_set_domain_grant`, `wiki_revoke_domain_grant` |
| Поддерживается code graph | `wiki_code_status`, `wiki_code_search`, `wiki_code_context` |
| Только hosted PostgreSQL | `wiki_code_publish_begin`, `wiki_code_publish_batch`, `wiki_code_publish_finalize`, `wiki_code_publish_abort` |
| Только локальный checkout | `wiki_code_index` |
| Только Git | `wiki_remediation_plan`, `wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`, `wiki_sync` |

Git-only инструменты возвращают
`{"error":"unsupported_storage","storage":"postgres","hint":"use this tool with Git storage"}`.
Три grant-инструмента вне hosted PostgreSQL возвращают `unsupported_transport` с
фактическими `storage` и `transport`. `wiki_create_domain(name)` требует
`can_create_domain`, атомарно создаёт домен, read/write grant creator-токена и строку
`can_manage_grants`, затем возвращает `created`, `already_existed`, `domain` и полный
effective scope сессии. Точный retry идемпотентен.

`wiki_list_domain_grants(domain)` показывает owner токена и content/management flags.
`wiki_set_domain_grant(domain, token_id, can_read, can_write)` и
`wiki_revoke_domain_grant(domain, token_id)` меняют только content grant другого
активного токена. Write требует read, пустой grant нужно revoke, self-target запрещён,
а management authority нельзя делегировать через HTTP: MCP schemas не имеют поля
записи management authority. После bootstrap выдавать это право может только CLI
recovery.

Hosted creation возвращает полный scope creator-токена:

```json
{"created":"new-project","already_existed":false,"domain":"new-project","read":["new-project"],"write":["new-project"],"primary":"new-project"}
```

Точный retry меняет только `already_existed` на `true`. Grant list возвращает
`{"domain":<domain>,"grants":[{"token_id":...,"owner":...,"can_read":...,"can_write":...,"can_manage_grants":...}]}`.
Set возвращает `domain`, `token_id`, `can_read`, `can_write`; revoke — `domain`,
`token_id`, `revoked`. Одиночный `tools/call`, отклонённый до dispatch — отсутствующее
право, malformed protected arguments или переданный клиентом `iwiki_id`, — возвращает
HTTP 200 с одной JSON-RPC ошибкой
`{"code":-32001,"message":"access_denied","data":{"hint":...}}`, чтобы MCP-клиент мог
сопоставить отказ с id своего запроса. Batch-запрос, отклонённый так же, остаётся на HTTP
403 `{"error":"access denied"}`, так как у batch нет одного id; отказы аутентификации,
origin и session тоже остаются на HTTP 401/403/404. Потеря права после dispatch, self-target и
foreign/missing state внутри транзакции дают HTTP 200 с in-band tool result
`{"error":"access_denied",...}`. Некорректные syntax/flags дают очищенную MCP/tool
validation failure.

PostgreSQL `wiki_status` сообщает `storage`, `transport`, эффективные `read`/`write`,
`primary` и видимые `domains`; локальный stdio также сообщает `project_dir`. DSN и
credentials не возвращаются:

```json
{"storage":"postgres","transport":"streamable-http","read":["backend"],"write":["backend"],"primary":"backend","domains":["backend"]}
```

PostgreSQL `wiki_read_page` возвращает optimistic revision вместе с authored Markdown.
Передайте это значение в update/delete:

```json
{"domain":"backend","slug":"architecture/auth","markdown":"# Auth\n\n## Flow\n...\n","revision":2}
```

Отсутствующая или проигравшая optimistic revision возвращает стабильные формы. Перед
повтором conflict снова прочитайте страницу:

```json
{"error":"expected_revision_required","hint":"read the page and retry with its revision"}
{"error":"conflict","current_revision":2,"hint":"read the page and retry against the current revision"}
```

Текущие non-goals: HTTP с Git storage, автоматический Git sync, создание БД или
extension, физическое удаление wiki и автоматическая миграция модели/размерности.

## Python code graph MVP

Опциональный code graph — отдельный локальный SQLite-кэш проекта, привязанного к
primary wiki-домену. Он индексирует Python, TypeScript/TSX и/или JavaScript-исходники,
в зависимости от настроенных `languages`, и не меняет `wiki_search` или Markdown/vector
индексы wiki. Пути кэша выводятся из wiki-base и primary domain:

```text
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.sqlite3
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.sqlite3-wal
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.sqlite3-shm
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.lock
<IWIKI_BASE_DIR>/.iwiki/code-<primary-domain>.metadata.json
```

Настройте его в `.iwiki.toml` привязанного проекта. Все значения необязательны;
ниже приведены defaults. `languages` принимает `python`, `typescript` и/или
`javascript`; значения `exclude` должны быть безопасными относительными путями.

```toml
[code_graph]
enabled = true
languages = ["python", "typescript", "javascript"]
auto_rebuild = "bounded"
max_rebuild_seconds = 10
max_full_rebuild_seconds = 10
max_file_bytes = 1000000
max_total_files = 20000
include_tests = true
exclude = []
```

`max_rebuild_seconds` ограничивает только bounded rebuild во время запроса.
`max_full_rebuild_seconds` ограничивает явный full build через `wiki_code_index` и по
умолчанию берёт значение `max_rebuild_seconds`, если не задан; на больших репозиториях
выставляйте его выше, чтобы full build не обрезался узким query-time бюджетом.
`typescript_type_boost` (по умолчанию `false`) включает изолированный,
best-effort-подпроцесс TypeScript Compiler API для резолвинга типов; его отсутствие
или сбой никогда не блокирует индексацию — Tree-sitter baseline всегда выполняется.

Поддерживаемые environment overrides: `IWIKI_CODE_GRAPH_ENABLED`,
`IWIKI_CODE_GRAPH_MAX_FILE_BYTES`, `IWIKI_CODE_GRAPH_MAX_FILES` и
`IWIKI_CODE_GRAPH_AUTO_REBUILD`. Сервер не строит code graph при startup.
`wiki_code_index` запрашивает полный rebuild; ограниченный rebuild во время запроса
возможен только при соответствующей настройке. Missing, incompatible, stale или
failed кэш возвращает typed diagnostics, не ломая обычные wiki-операции. Кэш
schema-v1 несовместим и заменяется детерминированным полным rebuild.

Сервер MCP предоставляет восемь code-graph инструментов; четыре инструмента
публикации описаны ниже в разделе распределённой публикации:

| Инструмент | Контракт |
| --- | --- |
| `wiki_code_status` | Возвращает настройку, состояние, freshness и diagnostics локального кэша. |
| `wiki_code_index` | Запрашивает полный rebuild для настроенных `languages`; `force` может перестроить уже current кэш. |
| `wiki_code_search` | Ищет typed file, module и symbol entities с optional kind, path, language и limit filters. |
| `wiki_code_context` | Расширяет точные typed entity-ID `seeds` через bounded relations; source по умолчанию выключен. |

`wiki_code_context` принимает только точные file/module/symbol entity IDs, возвращённые
code graph. Default: direction `both`, depth `1`, максимум 50 nodes, 20 files и
200000 source bytes. `include_source` по умолчанию `false`. Source discovery
отклоняет unsafe paths и symlink escapes; query и context безопасно завершаются,
если локальный кэш нельзя использовать.

Incremental indexing не входит в Python MVP; для него нужна отдельная specification и
delivery. Поддержка TypeScript — это статическое извлечение только через Tree-sitter
(декларации, импорты, class/interface heritage); члены interface не извлекаются, а
подпроцесс TypeScript Compiler API из `typescript_type_boost` — opt-in,
best-effort и пока не подключает реальную типовую информацию к резолвингу.

Поддержка JavaScript (расширения `.js`, `.jsx`, `.mjs`, `.cjs`) — это тоже статическое
извлечение только через Tree-sitter, с той же грамматикой `tsx`, что и у
TypeScript/TSX — синтаксический суперсет JavaScript, включая JSX, поэтому новая
зависимость парсера не добавлялась. В отличие от TypeScript, каждый JavaScript-файл
безусловно module-backed (без проверки на top-level import/export), потому что
CommonJS-файл, который только присваивает `module.exports`, всё равно должен быть
разрешимой целью импорта. Извлекаемые декларации: классы, методы, функции (включая
`async`), arrow- и function-выражения через `const`/`let`/`var`, методы object-literal
(shorthand и `key: function`/`key: arrow`), а также ES5 prototype-методы
(`C.prototype.m = ...`, только если `C` уже является символом, объявленным в том же
файле). Relations: `DECLARES`, `IMPORTS` (и ESM `import`, и CommonJS `require`, включая
деструктурированный `require`), `CALLS`, `INHERITS`. Относительный specifier
(`./util.js`) резолвится в project module с отброшенным расширением, с fallback
`<dir>.index` для импорта директории — именно это позволяет JavaScript-файлу
импортировать TypeScript-модуль. `wiki_code_context` принимает seeds `js:` и `ts:`, а
не только `py:`.

Приоритет дизайна JavaScript — доверие важнее покрытия: он никогда не создаёт
спекулятивное ребро. JS→TS импорты резолвятся, а TS→JS — нет: собственный резолвинг
импортов TypeScript не менялся и там остаётся unresolved. Нет вывода типов, нет
выполнения `node`/`tsc`/бандлера, `node_modules` не обходится. Алиасы путей из
tsconfig/jsconfig и карты `imports`/`exports` из `package.json` не читаются, поэтому
bare specifier остаётся unresolved. Динамический `require(expr)`, computed member
access (`o[k]()`), вызов вызова (`f()()`) и tagged template не создают ребро. Голый
вызов внутри метода класса или метода объектного литерала не привязывается к соседнему
члену, потому что сам JavaScript требует для этого `this.` или имя объекта: пробуются
только function-like охватывающая область и область модуля, а `this.m()` и `super.m()`
не извлекаются. Значение, импортированное как default export
(`import thing from './m'`), никогда не разворачивается в члены модуля: `thing` — это
default-экспортированное значение, форма которого статически неизвестна, поэтому
`thing.build()` не считается именованным экспортом `build` и остаётся unresolved.
Namespace-импорт (`import * as ns`) и целиком-модульный `const m = require('./m')`
разворачиваются, потому что оба действительно связывают объект модуля. Та же
неразворачиваемость затрагивает и `extends`: класс, наследующий default-импортированную
базу (`import Base from './base'; class X extends Base {}`), не создаёт
project-scoped, resolved ребро INHERITS — heritage-цель откатывается к
module-qualified имени в импортирующем файле и остаётся unresolved, точно так же, как
уже ведёт себя TypeScript-адаптер для любой импортированной heritage-цели.
Именованный импорт (`import { Base } from './base'`) по-прежнему резолвит INHERITS
между файлами. Известное ограничение: локальная переменная или параметр, которые
shadow-ят имя импорта, всё равно разворачиваются в импорт при построении цели вызова,
потому что резолвер не отслеживает реальную лексическую область видимости;
исправление этого выходит за рамки MVP.

### Распределённая публикация code graph

Code graph всегда строится из локального checkout, но полученный снапшот может жить в
другом месте. Машина с репозиторием индексирует его и публикует один неизменяемый
снапшот; сервер без checkout отвечает на `wiki_code_status`, `wiki_code_search` и
`wiki_code_context` из активного снапшота.

Выберите ровно одну цель публикации и одну цель чтения в `.iwiki.toml` привязанного
проекта. Fallback отсутствует: сбой выбранного режима возвращается вызывающей стороне и
никогда не повторяется через другой режим.

```toml
[code_graph]
publish_mode = "sqlite" # sqlite | postgres | mcp
read_mode = "sqlite"    # sqlite | postgres | mcp
max_snapshot_age_seconds = 86400 # 0 отключает отбраковку по возрасту
max_batch_rows = 1000
max_batch_bytes = 1000000
publication_session_ttl_seconds = 900
staging_retention_seconds = 86400
staging_cleanup_limit = 100
```

Опубликованный снапшот — а не конфигурация читающего сервера — определяет, какие языки
может вернуть hosted-чтение. `wiki_code_search` на хранилище PostgreSQL берёт языковой
фильтр из заголовка активного снапшота, пересечённого с языками, которые умеет
запрашивать текущий бинарник сервера, поэтому hosted-серверу не нужен собственный
`code_graph.languages`, а его каталог проекта может быть пустым. Поиск без фильтра
возвращает строки на всех языках, объявленных снапшотом; фильтр с языком, которого в
снапшоте нет, возвращает `{"error": ..., "code": "unsupported_language", "hint": "the
active snapshot declares: ..."}` (раньше — вводящий в заблуждение `invalid_config`), а
язык, который эта сборка не умеет парсить, по-прежнему даёт `invalid_config`. Язык,
объявленный снапшотом, но неизвестный бинарнику сервера, исключается из фильтра и
сообщается в `warnings` как `unknown_snapshot_language:<name>`. Публикация более широкого
набора языков соответственно расширяет то, что возвращают hosted-чтения этого домена.
Локальные `sqlite`-чтения не меняются: там авторитетен собственный
`code_graph.languages` проекта.

Готовый снапшот старше положительного `max_snapshot_age_seconds` возвращает
`stale_snapshot` без строк, при этом status продолжает сообщать возраст и отметки
времени. Значение `0` полностью отключает отбраковку по возрасту. Hosted-сервер
применяет собственные проверенные потолки для числовых полей; удалённый клиент не может
их поднять. Для `max_batch_rows` и `max_batch_bytes` в частности, `publish_mode = "mcp"`
узнаёт реальные лимиты сервера из ответа `wiki_code_publish_begin` и автоматически
подгоняет под них размер батчей — локальное значение в `.iwiki.toml`, большее
серверного, никогда не отправляется как есть, а отказ называет точный лимит и
полученное значение вместо голого `invalid_batch`.

Секреты никогда не попадают в `.iwiki.toml`. Режим MCP читает
`IWIKI_CODE_GRAPH_MCP_URL` и `IWIKI_CODE_GRAPH_MCP_TOKEN` только из окружения
исполнения, и оба отсутствуют в status, логах, заголовках снапшота, ошибках и repr
объектов. Прямой режим PostgreSQL переиспользует существующий блок `[storage]` и
требует `IWIKI_DB_PASSWORD`, `IWIKI_EMBED_MODEL` и `IWIKI_EMBED_DIMENSIONS` (plus
optional `IWIKI_RERANK_MODEL`, когда он настроен).

| Режим | Публикует в | Требует |
| --- | --- | --- |
| `sqlite` | Локальный кэш code graph рядом с базой wiki | Локальный checkout; нет mode-specific publication environment variables |
| `postgres` | Настроенную базу PostgreSQL wiki | Локальный checkout плюс `[storage]`, `IWIKI_DB_PASSWORD`, `IWIKI_EMBED_MODEL` и `IWIKI_EMBED_DIMENSIONS` (optional `IWIKI_RERANK_MODEL`) |
| `mcp` | Authenticated Streamable HTTP endpoint на той же машине или удалённый | Локальный checkout плюс `IWIKI_CODE_GRAPH_MCP_URL` и `IWIKI_CODE_GRAPH_MCP_TOKEN` |

`wiki_code_index` остаётся локальной операцией извлечения. На сервере без checkout он
возвращает `source_unavailable` и не создаёт ни сессии, ни снапшота; запускайте индексер
на машине с репозиторием. Один primary-домен соответствует ровно одному репозиторию.

Удалённая публикация — жизненный цикл из четырёх вызовов поверх существующей
авторизации по bearer-токену: `wiki_code_publish_begin`, повторяемый
`wiki_code_publish_batch`, затем `wiki_code_publish_finalize` или
`wiki_code_publish_abort`. Ни один из них не принимает поле арендатора или домена;
клиент привязывает каждую удалённую сессию к `primary` локального проекта (из
`.iwiki.toml`) вызовом `wiki_bind` сразу после `session.initialize()`, и сервер выводит
`iwiki_id` и связанный primary из этой сессии — поэтому токен обязан иметь право записи
в primary-домен проекта; `wiki_bind` только сужает уже выданный scope и не может его
расширить. Сессия принадлежит создавшей её личности: другой токен с правом записи в тот
же домен не может дополнить, прервать или завершить её, а процесс-замена обязан открыть
новую сессию.

Батчи несут только строки — никогда файл базы, текст исходников, абсолютный путь
checkout, учётные данные или сформированные издателем wiki-ссылки. Цель пересчитывает
ревизию полезной нагрузки, выводит ссылки code-to-wiki из собственного Markdown
назначения и активирует снапшот одним коммитом. Поэтому читатели видят либо предыдущую
полную ревизию, либо новую, но никогда частичную загрузку. Повтор принятого ordinal с
теми же строками идемпотентно успешен; повтор с другими строками возвращает
`batch_conflict`.

Повторяйте публикацию целиком после `busy`, `session_expired`, `snapshot_conflict`,
`revision_mismatch` или `markdown_unavailable`: откройте новую сессию и отправьте
заново. `snapshot_conflict` означает, что активный снапшот или Markdown назначения
изменились, пока сессия была открыта, поэтому перестроенный граф нужно публиковать
против текущего состояния. Истёкшие staging-сессии убираются ограниченными порциями при
открытии следующей сессии; фоновый демон не работает.

Для чтений PostgreSQL или удалённого MCP `include_source=true` возвращает контекст графа
без исходников плюс `source_unavailable`; сервер никогда не запрашивает исходники у
издателя. Локальные чтения SQLite сохраняют существующее защищённое поведение с
локальными исходниками. Лимиты search и context применяются для каждого адаптера чтения,
поэтому удалённый вызывающий не может запросить неограниченный результат или неявно
загрузить весь граф.

Первая публикация в пустой домен — обычная сессия: status сообщает `missing_snapshot`,
пока первый `finalize` не завершится успешно.

### Плановая публикация оператором

Запускайте publisher на машине, где находится checkout. Для каждого корректного
ровно-одного `publish_mode` (`sqlite`, `postgres` или `mcp`) используется одна команда:

```bash
iwiki-mcp code publish --project <checkout> [--json]
```

`sqlite` публикует в local target/cache под настроенным Git Wiki base по пути
`<wiki-base>/.iwiki/code-<domain>.sqlite3`; `postgres` использует существующую publisher
abstraction с настроенным прямым PostgreSQL binding, без raw SQL; `mcp` использует тот
же publication protocol через local или remote Streamable HTTP endpoint, заданный
`IWIKI_CODE_GRAPH_MCP_URL` и token. Local endpoint — это HTTP server на той же машине,
никогда не stdio. Local и remote HTTP publication эквивалентны: выбирайте цель,
заданную единственным `publish_mode`, и не придумывайте fallback. Только PostgreSQL
source cache остаётся локальным по пути `<project>/.iwiki/code-<domain>.sqlite3`,
исключается через `.git/info/exclude` и не является fallback target.

| Output | Значение | Exit status |
| --- | --- | --- |
| Text | Human-readable output format | Оба формата завершаются по outcome |
| `--json` | Compact machine-readable output format | Оба формата завершаются по outcome |

Text и `--json` выбирают только output format. Оба формата завершаются с `0`, когда
snapshot ready, с `1` при runtime/publication failure или с `2` при
usage/configuration failure.

Text stderr и compact JSON редактируют secrets и operational location data: не выводятся
password, token, URL, DSN или checkout path. `postgres` читает `IWIKI_DB_PASSWORD`; `mcp`
читает `IWIKI_CODE_GRAPH_MCP_URL` и `IWIKI_CODE_GRAPH_MCP_TOKEN` из защищённого runtime
environment. Для `postgres` также требуются `IWIKI_EMBED_MODEL` и
`IWIKI_EMBED_DIMENSIONS`; `IWIKI_RERANK_MODEL` optional, когда он настроен.

Настраивайте scheduling вне этого репозитория. Сохраните service как
`/etc/systemd/system/iwiki-codegraph-publisher.service`, timer как
`/etc/systemd/system/iwiki-codegraph-publisher.timer`. Protected environment file
должен быть root-owned mode `0600`; он передаёт `IWIKI_DB_PASSWORD`,
`IWIKI_EMBED_MODEL`, `IWIKI_EMBED_DIMENSIONS` и optional `IWIKI_RERANK_MODEL` без
значений в unit. Dedicated account `iwiki` должен иметь доступ к checkout.
Mode-specific EnvironmentFile contents: `postgres` использует `IWIKI_DB_PASSWORD`,
`IWIKI_EMBED_MODEL` и `IWIKI_EMBED_DIMENSIONS` (optional `IWIKI_RERANK_MODEL`); `mcp`
использует `IWIKI_CODE_GRAPH_MCP_URL` и `IWIKI_CODE_GRAPH_MCP_TOKEN`; `sqlite` не
требует mode-specific publication variables.

```ini
[Unit]
Description=Publish iwiki code graph

[Service]
Type=oneshot
User=iwiki
WorkingDirectory=/srv/project
EnvironmentFile=/etc/iwiki/codegraph-publisher.env
ExecStart=/usr/local/bin/iwiki-mcp code publish --project /srv/project --json
```

```ini
[Unit]
Description=Schedule iwiki code graph publication

[Timer]
OnCalendar=hourly
Persistent=true
Unit=iwiki-codegraph-publisher.service

[Install]
WantedBy=timers.target
```

Для любого CI provider сделайте protected secret variables доступными окружению job и
запустите ту же команду; документация намеренно не добавляет provider workflow file:

```bash
export IWIKI_DB_PASSWORD
export IWIKI_EMBED_MODEL
export IWIKI_EMBED_DIMENSIONS
export IWIKI_CODE_GRAPH_MCP_URL
export IWIKI_CODE_GRAPH_MCP_TOKEN
iwiki-mcp code publish --project <checkout> --json
```

Перед `wiki_code_search` или `wiki_code_context` проверьте, что `wiki_code_status`
сообщает `fresh == true`. Когда нужна только Markdown-семантика wiki, отдельно
используйте `wiki_search`. Поддерживаемая ежедневная последовательность: `wiki_search → wiki_code_search → wiki_code_context`. Unified wiki/code search остаётся будущей возможностью и не реализован. `wiki_unified_search` намеренно не зарегистрирован, поскольку quality evidence вернул `do_not_implement`; см. [отчёт оценки](superpowers/evidence/wiki-unified-search-evaluation.md)
и [машинно-читаемые данные](superpowers/evidence/wiki-unified-search-evaluation.json).

### Профили снапшота SQLite и неопределённость коммита

Локальный кэш SQLite имеет ровно два принимаемых профиля схемы v2. Legacy-профиль
содержит пять публичных таблиц сущностей и требует строгой проверки storage stamp по
базе плюс sidecar. Профиль публикации добавляет внутреннюю таблицу
`code_graph_publication`, которая и несёт авторитетное свидетельство готовности; в этом
профиле `.metadata.json` — только кэш, он может отсутствовать, устареть или быть
пересоздан без изменения готовности.

Публикация SQLite может вернуть `commit_uncertain`. Это означает, что каноническая
замена могла произойти, но устойчивость каталога не подтверждена. Она не утверждает ни
успех, ни откат и допускает ровно одно восстановление: повтор `finalize` в том же
процессе. Batch, abort, автоматический откат и подмена адаптера запрещены. Если процесс
потерян до сверки, проверьте `wiki_code_status` и откройте новую сессию. Прямой
PostgreSQL и удалённый MCP никогда не выдают `commit_uncertain`.

Перед откатом на бинарь до публикации сохраните или восстановите legacy-снапшот либо
переиндексируйте этим бинарём, поскольку он может отвергнуть внутреннюю таблицу.

### Code graph benchmark

Запустите offline release evidence из корня репозитория:

```bash
uv run python -m eval.code_graph --fixture-root tests/fixtures/codegraph --output /tmp/iwiki-code-graph-evidence
```

Команда записывает JSON и Markdown reports в output directory. Warm maximum каждого
search case должен быть ниже `<500 ms`; это blocking first-release gate. Сравнение
с `<150 ms` только reportится как non-blocking post-v1 target. Иной blocking miss
записывает evidence и завершается nonzero.

## Бенчмарк search pipeline

Bounded fusion benchmark в `eval/search_pipeline/` предназначен только для evaluation: он не меняет production-поиск, production fusion weights и production rerank settings. Изменение rerank-budget отложено.

Воспроизведите существующие evidence без credentials:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out <report-dir> --pareto --replay-evidence <evidence.json>
```

Только после успешного replay запросите подтверждение оператора перед live benchmark. Live-команда использует созданный оператором environment file; не читайте и не копируйте его credentials в репозиторий:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out <report-dir> --modes hybrid,lexical,semantic --pareto --env-file <operator-env-file>
```

### Hard-negative gate

Активация hard-negative в решении bounded fusion выводится из захваченного
baseline. Проверяются два рассмотренных контракта hard-negative; по этому baseline
каждый получает состояние `active`, `unavailable` или `invalid`. Candidate может
пройти hard-negative gate, только когда active как минимум два контракта.

`hard_negative_evidence_invalid` означает, что baseline evidence одного или
нескольких рассмотренных контрактов некорректен.
`hard_negative_evidence_incomplete` означает, что evidence корректен, но active
меньше двух контрактов. Эти диагностические причины отличаются от rejection
качества candidate после проверки gate. Absolute ranks используются только для
диагностики; production search behavior, fusion weights и rerank settings не меняются.

### Требования

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

Клиент запускает сервер с `cwd` в корне проекта, поэтому `.iwiki.toml` (см. [Привязка проекта](#привязка-проекта)) подхватывается автоматически.

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

## База и домены

`IWIKI_BASE_DIR` указывает на общую wiki-базу. База предполагается git-репозиторием, чтобы записи можно было коммитить и синхронизировать между машинами или проектами.

Каждый домен — это подкаталог внутри базы. Идентичность страницы — это её путь `<type>/<slug>`, относительный домену: `wiki_write_page` кладёт файл в каталог, названный по её (разрешённому) фронтматтер-полю `type`, и то же значение `<type>/<slug>` — без суффикса `.md` — возвращает `wiki_list_pages` и ожидает как `slug` каждый из `wiki_read_page` / `wiki_update_page` / `wiki_delete_page`. Переносимые векторное хранилище домена (`index.jsonl`) и лог ingest (`log.jsonl`) хранятся в корне домена; устаревший домен с `.iwiki/index.jsonl` / `.iwiki/log.jsonl` автоматически мигрирует в корень при первом же обращении любого инструмента. Базовый `.iwiki/graph.sqlite3` — отдельный локальный пересобираемый SQLite-кэш, исключённый из Git вместе с WAL/SHM; `.iwiki/lock` — межпроцессная git-блокировка.

```text
/home/user/wiki/
  .iwiki/
    graph.sqlite3        # локальный производный кэш, не коммитится
    lock
  backend/
    architecture/
      auth.md
    guide/
      onboarding.md
    index.jsonl
    log.jsonl
  frontend/
    concept/
      routing.md
    index.jsonl
    log.jsonl
```

Используйте одну базу для всех проектов. Привязывайте каждый проект к доменам, из которых он читает, и к домену, в который пишет.

## Графовый кэш и ссылки

Графовый кэш хранит направленные ссылки страниц и anchors в SQLite, но поиск обходит его как ограниченную ненаправленную окрестность только внутри видимой read-области. Он не смешивает зависимости кода и wiki. После clone, pull, повреждения или расхождения fingerprint сервер пересобирает затронутый локальный кэш из Markdown без embedding-вызовов; пока кэш недоступен, используется безопасный Markdown fallback. Сбой обновления графа не отменяет коммит Markdown, `index.jsonl` или `log.jsonl`: затронутые домены помечаются `dirty`, а fingerprint-проверяемый Markdown fallback остаётся authoritative до успешного локального ремонта.

Внутри одного домена используйте относительную Markdown-ссылку: `[Auth](architecture/auth.md#flow)`. На страницу другого видимого домена ссылайтесь только canonical URI: `[Routing](iwiki://frontend/concept/routing#flow)`. Корневые `index.md` и `log.md` — генерируемые OKF-артефакты, а не страницы графа или цели обхода; `wiki_lint` сообщает ссылку на них как `reserved_target`.

## Привязка проекта

Сервер определяет привязку проекта из `.iwiki.toml` в корне проекта. Клиент обычно запускает сервер с `cwd`, равным корню проекта; переопределить это можно через `IWIKI_PROJECT_DIR` или `iwiki-mcp --project DIR`.

Если `.iwiki.toml` отсутствует или содержит только пробельные символы, сервер
создаёт комментированный шаблон с примерами Git, PostgreSQL и `code_graph`.
Аналогично создаётся `.iwikiignore`: шаблон содержит секреты и типичный проектный
шум и при наличии дополняется текущим `.gitignore`. После появления любого
непробельного содержимого сервер оставляет байты обоих файлов без изменений.
После инициализации редактируйте их только вручную.

```toml
# .iwiki.toml
read = ["backend", "frontend"]
write = ["backend", "frontend"]
primary = "backend"
# base = "/home/user/wiki"
```

`read` задаёт область поиска по умолчанию для проекта. Чтобы читать из **всех** доменов базы, укажите `read = []` или вовсе уберите строку — пустой или отсутствующий `read` откатывается ко всем доменам. `read = ["all"]` **не** является подстановкой: значение трактуется как домен с буквальным именем `all`. `write` — список доменов, которые могут менять mutating-инструменты. `primary` задаёт основной домен для инструментов без явного `domain`, например `wiki_index`; он должен входить в `write`. Каждый домен из `write` должен также входить в `read`. `base` опционален и переопределяет `IWIKI_BASE_DIR` для этого проекта.

Для Git storage `wiki_bind` не записывает конфигурацию проекта. Попытка
автоматически изменить привязку возвращает контролируемый ответ и не меняет файл:

```json
{"error":"project configuration cannot be changed automatically","code":"project_config_manual_edit_required","hint":"edit .iwiki.toml manually; populated configuration is never rewritten automatically"}
```

Для PostgreSQL `wiki_bind` по-прежнему только сужает область текущей сессии и
никогда не меняет `.iwiki.toml`. `wiki_create_domain` может bootstrap-нуть пустой
отсутствующий Git-домен вне текущего списка write; он не создаёт страницу, индекс
или лог. Перед первой записью добавьте домен в `.iwiki.toml` вручную.

## Научите агента пользоваться iwiki

Регистрация сервера открывает инструменты, но агенту всё ещё нужны указания, *когда* их вызывать. В репозитории есть готовые сниппеты в [`templates/`](../templates):

- `templates/CLAUDE.md.snippet` — добавьте в `CLAUDE.md` проекта (Claude Code).
- `templates/AGENTS.md.snippet` — добавьте в `AGENTS.md` проекта (Codex).

Оба несут одинаковые указания: искать перед задачей, не менять привязку при обычном старте проекта, писать страницы после изменений функциональности и вызывать `wiki_sync` в конце сессии. Добавьте нужный сниппет один раз на проект:

```bash
cat templates/CLAUDE.md.snippet >> CLAUDE.md   # Claude Code
cat templates/AGENTS.md.snippet >> AGENTS.md   # Codex
```

Сниппеты ссылаются на `.iwiki.toml`, поэтому сначала привяжите проект (выше).

## Справочник переменных окружения

**Обязательные**

| Переменная | По умолчанию | Значение |
|---|---|---|
| `IWIKI_LLM_BASE_URL` | нет | Базовый URL OpenAI-совместимого endpoint эмбеддингов, обычно заканчивается на `/v1`. |
| `IWIKI_LLM_KEY` | нет | API-ключ для endpoint эмбеддингов. |

**Модель эмбеддингов**

| Переменная | По умолчанию | Значение |
|---|---|---|
| `IWIKI_EMBED_MODEL` | `text-embedding-3-small` | Имя модели эмбеддингов. |
| `IWIKI_EMBED_DIMENSIONS` | `1536` | Размер вектора. Должен соответствовать выбранной модели эмбеддингов. |

**Модель чата**

| Переменная | По умолчанию | Значение |
|---|---|---|
| `IWIKI_CHAT_MODEL` | пусто | Опциональное имя чат-модели для серверной классификации `type`/`tags`. Использует те же `IWIKI_LLM_BASE_URL` и `IWIKI_LLM_KEY`. Если не задан, фронтматтер по умолчанию имеет значение `type="concept"` без тегов. |

**Жизненный цикл сервера**

| Переменная | По умолчанию | Значение |
|---|---|---|
| `IWIKI_IDLE_TIMEOUT_SECONDS` | `1800` | Завершает stdio MCP-процесс после указанного числа секунд без входящей MCP-активности. `0` отключает лимит. Выполняющийся вызов инструмента завершается до остановки; для следующего вызова клиент должен переподключиться или запустить новый MCP-процесс. |

**Настройка поиска**

| Переменная | По умолчанию | Значение |
|---|---|---|
| `IWIKI_TOP_K` | `8` | Максимум результатов по умолчанию для поиска и поиска связанных секций. |
| `IWIKI_SCORE_THRESHOLD` | `0.2` | Минимальное векторное сходство по умолчанию для возвращаемой секции. |
| `IWIKI_SEARCH_MODE` | `hybrid` | Режим для опущенного `wiki_search.mode`: `hybrid`, `lexical` или `semantic`. Регистр и пробелы нормализуются; явный режим имеет приоритет. |
| `IWIKI_RERANK_MODEL` | пусто | Опциональная LiteLLM-совместимая reranker-модель. Использует `IWIKI_LLM_BASE_URL` / `IWIKI_LLM_KEY`, оценивает полный batch с timeout 60 секунд, ограничивает только число строк в ответе provider финальным числом результатов и при сбое возвращает только очищенные метаданные. |
| `IWIKI_GRAPH_DEPTH` | `2` | Глубина переходов по wiki-ссылкам для graph-расширения поиска и поиска связанных секций. |
| `IWIKI_SEED_TOP_K` | `5` | Сколько статей отбирает проход по summary-векторам до graph-расширения. |
| `IWIKI_BFS_TOP_K` | `10` | Ограничение на graph-расширенные (не-seed) статьи, добавляемые в пул кандидатов. |
| `IWIKI_SEED_THRESHOLD` | `0.15` | Минимальное сходство summary-вектора, чтобы статья стала seed-кандидатом. |
| `IWIKI_WRITE_SEED_THRESHOLD` | `0.35` | Минимальное сходство summary-вектора для отбора seed в точном write-target поиске `wiki_search(intent="write")`. Выше, чем `IWIKI_SEED_THRESHOLD`, чтобы неродственная страница не предлагалась как цель для upsert. |

**Индексирование**

| Переменная | По умолчанию | Значение |
|---|---|---|
| `IWIKI_CHUNK_SIZE` | `512` | Целевое число токенов на индексируемый чанк. |
| `IWIKI_CHUNK_OVERLAP` | `64` | Перекрытие токенов между соседними чанками. |
| `IWIKI_SUMMARY_MAX_CHARS` | `400` | Максимальная длина сводки страницы. |

**Расположение**

| Переменная | По умолчанию | Значение |
|---|---|---|
| `IWIKI_BASE_DIR` | нет | Каталог общей wiki-базы. Может переопределяться полем `base` в `.iwiki.toml`. |
| `IWIKI_PROJECT_DIR` | `cwd` процесса | Каталог проекта для чтения `.iwiki.toml`. Может переопределяться через `--project DIR`. |

## Инструменты

| Инструмент | Что делает |
|---|---|
| `wiki_search` | Режимы чтения — только `hybrid`, `lexical` и `semantic`; явный режим переопределяет `IWIKI_SEARCH_MODE` (по умолчанию `hybrid`), а публичный `vector` отклоняется. Summary-семантика страниц, lexical-совпадения страниц, graph-страницы, глобальные semantic-чанки и lexical-секции ранжируются независимо и объединяются RRF до финального top-k. Результаты содержат `hit` (`semantic`/`lexical`/`both`) и `source` (`seed`/`graph`/`global`/`lexical`). При заданном `IWIKI_RERANK_MODEL` точные актуальные чанки полного пула отправляются одним аутентифицированным LiteLLM batch с timeout 60 секунд, а provider `top_n` ограничивается запрошенным финальным `k`; сбой сохраняет предварительный порядок и возвращает только очищенные метаданные `rerank`. `scope`, `domains`, `k`, `threshold`, `type` и `tags` ограничивают read-поиск. `intent="write"` остаётся изолированным summary-vector поиском write-target и игнорирует read-режим/reranking. |
| `wiki_read_page` | Прочитать одну Markdown-страницу по домену и slug. С `heading` вернуть только одну секцию `##` (вместе с её `section_hash`) вместо всей страницы. |
| `wiki_list_pages` | Перечислить slug-и и файлы страниц в домене. |
| `wiki_related` | Вернуть связанные секции для id секции в пределах одного домена; форма `{"vector": [], "graph": []}` и domain-local fallback не меняются. |
| `wiki_write_page` | Проверить и записать новую страницу, проиндексировать домен и вернуть, удался ли авто-коммит базы. |
| `wiki_update_page` | Заменить тело одной существующей секции `##`. С `new_heading` переименовать заголовок и атомарно переписать точные видимые входящие ссылки, если их домены доступны для записи. Принимает `expected_section_hash` для оптимистичной конкурентности. |
| `wiki_insert_section` | Вставить новую секцию `##` (позиция задаётся `after_heading` / `before_heading`) без переписывания остальной страницы. |
| `wiki_delete_section` | Удалить одну существующую секцию `##` без переписывания остальной страницы. Принимает `expected_section_hash`. |
| `wiki_move_section` | Переместить одну существующую секцию `##` (позиция задаётся `after_heading` / `before_heading`) без изменения её тела. Принимает `expected_section_hash`. |
| `wiki_delete_page` | Удалить страницу, записать delete-op, переиндексировать и синхронизировать базу. |
| `wiki_index` | Пересобрать индекс одного домена; по умолчанию использует привязанный домен write, если опущено. |
| `wiki_list_domains` | Перечислить видимые каталоги доменов в базе с размерами индексов. |
| `wiki_create_domain` | Создать пустой каталог домена и вернуть, удался ли авто-коммит базы; `index.jsonl` / `log.jsonl` домена создаются лениво в его корне при первой записи или переиндексации. |
| `wiki_bind` | Сузить PostgreSQL-область текущей сессии; для Git вернуть `project_config_manual_edit_required`, изменения выполняются вручную. |
| `wiki_status` | Показать разрешённую базу, каталог проекта, домены read, домен write и доступные домены. |
| `wiki_lint` | Read-only Markdown-authoritative отчёт: битые/reserved/unavailable-domain ссылки, сироты, stale-страницы, `missing_source` и пробелы секций, а также независимый per-domain SQLite graph parity (`state`, fingerprint, страницы, рёбра, anchors). Он не создаёт и не пересобирает кэш; non-ready или mismatch добавляет подсказку `wiki_index`. |
| `wiki_remediation_plan` | Сгруппировать текущие lint-находки в read-only план update/delete действий. |
| `wiki_migrate_okf` | Добавить OKF-фронтматтер и нормализовать type-каталоги автономно либо вернуть план для ревью. |
| `wiki_apply_okf` | Применить подтверждённые OKF-метаданные и решения по layout; при переносе страницы между type-каталогами атомарно переписать точные видимые входящие ссылки. |
| `wiki_export_okf` | Выполнить детерминированный in-place OKF sweep и пересоздать корневые `index.md` / `log.md`. |
| `wiki_sync` | Выполнить `git pull --rebase` и `git push` в базе. |

`wiki_write_page` отказывается перезаписывать существующую страницу. Для правки одной существующей секции используйте `wiki_update_page(domain, slug, heading, new_body, source=None, new_heading=None)`. `new_heading` необязателен: без него это обычное обновление одного домена; с ним сервер переписывает точные входящие относительные ссылки домена страницы и точные `iwiki://` ссылки видимых read-доменов. `wiki_apply_okf` использует ту же транзакцию только когда смена `type` переносит страницу. `wiki_insert_section` и `wiki_delete_section` добавляют или удаляют одну секцию `##`, а `wiki_move_section` переставляет одну секцию — все три без переписывания остальной страницы. `wiki_update_page`, `wiki_delete_section` и `wiki_move_section` принимают `expected_section_hash` (полученный из предыдущего `wiki_read_page(..., heading=...)`) для оптимистичной конкурентности: устаревший hash отклоняется с `section_conflict` вместо молчаливой перезаписи параллельной правки.

Междоменная операция начинается, только если каждый найденный видимый referrer входит в `write`; видимый read-only referrer блокирует её до изменения Markdown. Скрытые домены не инспектируются и не репортятся, поэтому никогда не переписываются. Результат дополняется полями `transaction_id`, `rewritten_pages`, `affected_domains` и `rewritten_links`.

Каждая междоменная операция держит блокировку mutation базы, stage-ит только затронутые Markdown и доменные `index.jsonl` / `log.jsonl` в корне, затем делает один локальный commit с `Iwiki-Transaction: <id>`. Её fsync-журнал находится в `.iwiki/transactions/<id>` и проходит `prepared` → `applied` → `committed` → `finalized`. Прерывание до commit восстанавливает snapshot; после commit — восстанавливает/помечает производный graph и финализирует журнал до следующей перекрывающейся мутации. Неоднозначное восстановление возвращает `manual_recovery_required`. Push остаётся fail-soft: локальный commit и переносимые authoritative-файлы остаются при ошибке публикации.

Разрешение project-relative stale-source остаётся отдельным audited follow-up; этот релиз не меняет его скрыто.

Сервер также предоставляет MCP-ресурс `iwiki://authoring-rules` с правилами структуры страниц.

## Pareto-бенчмарк

Запустите только оценочный Pareto-эксперимент на live-размеченном корпусе поиска:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out ./pareto-evidence --env-file /path/to/operator.env --pareto
```

`--env-file` читает созданный оператором файл окружения только для этого процесса; он
не создаёт, не изменяет и не записывает в файл учётные данные. Храните файл вне
каталога отчётов и вне контроля версий. В отчёты попадают только очищенные данные.

`--pareto` - команда оценки, а не переключатель production-конфигурации. Production
константы fusion-весов или rerank-batch применяются только если отчёт содержит
прошедшую рекомендацию для соответствующих quality- и latency-gate. Решение
`needs_work`, включая `no_passing_weight_map`, оставляет production-поведение поиска
без изменений.

## Совместимость с OKF

Каждая страница несёт небольшой блок YAML-фронтматтера над `# Title` H1, который автоматически записывают `wiki_write_page` / `wiki_update_page` / `wiki_apply_okf`. Поля:

| Поле | Значение |
|---|---|
| `type` | Обязательное. **Открытый** словарь: предпочтительны `architecture`, `api`, `guide`, `reference`, `runbook`, `concept` (по умолчанию), но допустимо любое значение (например, `person`); значения вне списка получают лишь рекомендательный `unknown_type`. Также определяет каталог страницы: `wiki_write_page` кладёт файл по пути `<type>/<slug>.md` в корне домена — «голый» `slug` дополняется разрешённым `type` спереди, а `slug`, уже несущий ведущий сегмент, должен ему соответствовать. |
| `title` | Берётся из заголовка `# Title` страницы. |
| `description` | Авторский обзор статьи — единственный источник резюме, хранится как отдельный summary-вектор для отбора страниц. Он не добавляется к векторам секций и хранится целиком (без обрезки). Переходно берётся из секции `## Overview` только при миграции. |
| `resource` | Значение `source`, переданное инструменту записи, если оно было; `wiki_apply_okf` и `wiki_migrate_okf` при отсутствии `source` берут последний зарегистрированный в логе источник страницы. Хранимый путь — относительно проекта: абсолютный путь внутри проекта приводится к относительному, а любой путь (абсолютный или относительный, например `../../etc/hosts`), выходящий за пределы проекта, отклоняется. |
| `tags` | Метки в нижнем регистре в формате kebab-case, не более 5 на страницу. |
| `status` | Опциональное расширение iwiki: `stub` (по умолчанию), `developing`, `stable`, `deprecated`. |
| `timestamp` | При создании (`wiki_write_page`, `wiki_apply_okf`, `wiki_migrate_okf`): дата последнего git-коммита файла страницы, либо сегодняшняя дата, если ещё не закоммичена. При редактировании (`wiki_update_page`): всегда сегодняшняя дата. |

Зарезервированные OKF-файлы `index.md` (навигация) и `log.md` (история) генерируются только по запросу: `wiki_write_page` / `wiki_update_page` / `wiki_delete_page` больше не перегенерируют их при каждом изменении. Запустите `wiki_export_okf`, чтобы (пере)создать актуальные `index.md` / `log.md` в корне домена перед тем, как считать домен полным OKF-пакетом для внешнего потребителя. `index`/`log` зарезервированы только в корне домена — `wiki_write_page` отклоняет именно эти два полных идентификатора; слаг вида `concept/index` в типовом каталоге — обычная, отдельная страница и разрешён.

Страницы больше не содержат секцию `## Overview`: резюме хранится в `description`.
Связи-ссылки размещаются в двух зарезервированных секциях `##` — `## Outgoing links`
(Markdown-ссылки) и `## External links` (голые URL) — которые исключены из поискового
индекса, но по-прежнему питают граф ссылок. Запустите `wiki_export_okf` один раз для
миграции старых страниц (снимает `## Overview`, заполняет `description`, ставит `status`).

`type` и `tags` разрешаются в таком порядке приоритета: явный аргумент `type`/`tags` у инструмента записи побеждает; иначе, если задан `IWIKI_CHAT_MODEL`, сервер классифицирует тело страницы этой чат-моделью; иначе используется значение по умолчанию `type="concept"` без тегов.

Фасетный поиск сужает `wiki_search` по `type` и/или набору `tags`; значения запроса нормализуются так же, как хранимый фронтматтер (регистронезависимый `type`, kebab-case `tags`), поэтому `type="API"` по-прежнему совпадёт со страницей, где `type: api`:

```text
wiki_search(query="deploy steps", type="runbook", tags=["ci"])
```

Инструменты для внедрения OKF-фронтматтера в существующий домен:

| Инструмент | Что делает |
|---|---|
| `wiki_migrate_okf(domain=None)` | Добавляет фронтматтер всем страницам, у которых его нет. Два режима: **автономный** (пишет фронтматтер сразу), если задан `IWIKI_CHAT_MODEL`; иначе возвращает **план** — список кандидатов с выведенными title/description/timestamp и существующим словарём тегов домена — для классификации и применения вызывающим агентом. В автономном режиме `resource` каждой страницы при отсутствии источника берётся из последней записи лога, а теги, придуманные для одной страницы, переиспользуются как словарь для следующих страниц в этом же запуске. В обоих режимах инструмент также детерминированно переносит любую плоскую страницу (голый `<slug>.md` в корне домена), у которой уже есть фронтматтер `type`, под `<type>/<slug>.md`, переписывая внутридоменные ссылки; страница, чья целевая позиция уже занята, пропускается и отражается в `layout_collisions`, а не перезаписывается; страница, чей фронтматтер `type` не сводится к безопасному одиночному сегменту пути (например, содержит `/` или `..`), остаётся на месте и отражается в `layout_skipped_unsafe`. |
| `wiki_apply_okf(domain, slug, type, tags)` | Применяет классифицированные агентом `type`/`tags` (плюс выведенные поля) как фронтматтер одной страницы, переиндексирует, коммитит и пушит. Если `tags` не переданы, существующие теги страницы сохраняются, а не очищаются; существующие `description` и `status` всегда сохраняются без изменений. |
| `wiki_export_okf(domain=None)` | Проход по всему домену, приводящий его к OKF **на месте** (без копии, без `dest`): переписывает остаточные `[[wikilink]]` в Markdown-ссылки и гарантирует фронтматтер на каждой странице (детерминированно `type: concept` там, где его нет; существующие `type`/`tags` сохраняются), затем перегенерирует зарезервированные `index.md` / `log.md`. Детерминирован — чат-модель не вызывает. Возвращает `fixed_links`, `added_frontmatter` и `still_missing_frontmatter` / `still_legacy_wikilink`, плюс `next_steps` к `wiki_migrate_okf` для более точных `type`/`tags`. Каталог домена сам является OKF-пакетом. Также мигрирует каждую страницу к модели тела v2: снимает секцию `## Overview`, заполняя из неё `description` при пустом значении, и ставит `status` в `stub`. |

`IWIKI_CHAT_MODEL` (по умолчанию: пусто) опционален; если не задан, серверная классификация отключена и `wiki_migrate_okf` работает в режиме плана.

## Git-синхронизация базы

Когда `IWIKI_BASE_DIR` — git-репозиторий, `wiki_write_page` и `wiki_create_domain` после успешных изменений добавляют и коммитят базу. Если база не git-репозиторий, запись или создание всё равно успешно проходят на диске, а ответ инструмента возвращает `committed: false`. Используйте `wiki_sync`, `wiki_status` или команды git в репозитории базы для диагностики настройки репозитория и удалённого хранилища.

Используйте `wiki_sync`, чтобы делиться базой:

```text
wiki_sync()
```

`wiki_sync` выполняет `git pull --rebase`, затем `git push` в базе. При восстанавливаемых удалённых сбоях (`non_fast_forward`, `credential_unavailable` и `transport_unavailable`) стандартная последовательность Git pull/push повторяется не более трёх sync-попыток с паузой 250 мс. Ответ содержит `sync_attempts` и `push_attempts`; классифицированные сбои pull/push также содержат `failure_class`. Это поле может отсутствовать для результатов до удалённой попытки, включая случай не-git базы, отсутствие remote или тайм-аут блокировки. Неудачный push остаётся fail-soft предупреждением и не отменяет локальный коммит. Сервер не меняет Git-конфигурацию клиента, не загружает shell-профили, не ищет сокеты аутентификации и не выступает брокером учётных данных.

Git запускается неинтерактивно (`GIT_TERMINAL_PROMPT=0`, закрытый stdin), поэтому учётные данные должны быть заранее доступны процессу MCP через стандартные механизмы Git. Наличие credential helper в интерактивном shell само по себе не доказывает, что процесс MCP может его использовать. Если учётные данные недоступны, настройте неинтерактивный helper для пользователя сервера и выбранного транспорта, запустите MCP-сервер из окружения с нужным credential context либо выполните `wiki_sync` из доверенного терминала с таким контекстом. Не помещайте токены, пароли, remote URL со встроенными учётными данными или пути к сокетам аутентификации в конфигурацию или логи MCP.

Если `pull --rebase` конфликтует, `wiki_sync` прерывает rebase и возвращает `conflict: true`, `failure_class: rebase_conflict`, метаданные попыток и подсказку. Конфликты автоматически не повторяются: разрешите их вручную в репозитории базы. Если затронуты сгенерированные файлы индекса, пересоберите индексы нужных доменов через `wiki_index`, при необходимости закоммитьте пересобранные файлы в репозитории базы, затем снова запустите `wiki_sync`.

## Быстрый старт

1. Установите `iwiki-mcp` и зарегистрируйте его в Claude Code или Codex с `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY` и `IWIKI_BASE_DIR`.
2. В сессии агента создайте домен:

```text
wiki_create_domain(name="backend")
```

3. Вручную отредактируйте созданный `.iwiki.toml`, затем добавьте сниппет для агента (см. [Научите агента пользоваться iwiki](#научите-агента-пользоваться-iwiki)):

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

## Ограничения (v1)

- Внутри домена используйте `[Heading](<type>/<slug>.md#heading)`; между доменами — `iwiki://<domain>/<page-id>#<anchor>`.
- `.iwiki/graph.sqlite3` — локальный производный кэш, а не переносимая замена векторам/логам и не граф code-dependencies.
- Git storage использует numpy brute-force поиск по переносимым JSONL-индексам;
  PostgreSQL storage получает tenant/domain-scoped cosine-кандидатов через pgvector,
  затем применяет общие lexical fusion, deduplication и опциональный reranking.
- Проверки устаревания локальны для проекта и зависят от доступных путей к исходникам и логов ingest.
