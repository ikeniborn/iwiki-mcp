# Режимы хранения и транспорта

*Часть [документации iwiki-mcp](README.ru.md#документация). English version: [storage-modes.md](storage-modes.md).*

| Хранилище | stdio | Streamable HTTP |
| --- | --- | --- |
| Git-каталог | поддерживается; по умолчанию | не поддерживается |
| PostgreSQL | поддерживается для одной локально настроенной wiki | поддерживается для hosted-доступа к нескольким wiki |

## Локальный Git stdio

Существующий локальный режим не изменился:

```bash
export IWIKI_BASE_DIR=/srv/iwiki-base
iwiki-mcp --project /srv/project
```

## Локальный PostgreSQL stdio

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

## Hosted Streamable HTTP

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

## Поддерживаемый application container

Production-развёртывание использует корневой `compose.yaml` как один hardened
application service с тремя supervised-процессами: hosted MCP на
`127.0.0.1:8765`, nginx на выбранном оператором LAN/Traefik listener и
`iwiki-telegram-bot`. На хосте нужны ровно эти файлы:

```text
/opt/iwiki-mcp/server.toml       endpoint hosted MCP и внешнего PostgreSQL
/opt/iwiki-mcp/nginx.conf        LAN/Traefik listener и loopback upstream
/opt/iwiki-mcp/runtime.env       owner-only secrets и настройки бота
```

PostgreSQL остаётся внешней долговечной службой под управлением оператора. Контейнер БД
на том же хосте обязан публиковать host-порт, например `127.0.0.1:55432`; удалённая БД
задаёт собственные host и custom port и должна использовать
`sslmode = "verify-full"`. Этот Compose-проект и его runtime не создают PostgreSQL
service, базу или объекты схемы и не запускают миграции. Оператор заранее применяет
точную совместимую схему через административный/migrator path репозитория.
Конфигурация, HTTPS proxy routing, проверка на изолированном хосте, миграция, cutover и
rollback описаны в [deployment runbook](deployment.md). Production использует host
networking и фиксированный MCP listener `127.0.0.1:8765`, поэтому полный combined
container нельзя параллельно проверить на том же хосте; без отдельного хоста или VM
планируйте maintenance downtime и сохраняйте старые службы для rollback.

Сервер открывает ограниченный connection pool и применяет заданные statement/lock
timeouts. До открытия listener startup проверяет модель, её метаданные, точную версию
схемы и подготовленный runtime principal; миграции он никогда не запускает. Одна БД
хранит несколько изолированных wiki с разными `iwiki_id`. Модель эмбеддингов и
размерность — общие метаданные БД: несовпадение останавливает startup; их смена —
операторская миграция, не автоматический re-embedding. Embedding/rerank credentials
остаются только на сервере.

Каждый запрос заново читает текущие права токена. Сессия хранит явно выбранный
`selected` scope отдельно от пересечённого со свежими grants `effective` scope:
revocation действует на следующем запросе, восстановленное право возвращается только
если домен оставался selected, а новый grant целевого токена сам по себе не расширяет
существующую сессию. Только успешный `wiki_create_domain` расширяет текущую сессию
creator-токена; любой другой grant выбирается явно, потому что hosted `wiki_bind`
авторизуется против текущих grants токена, а не против текущего выбора сессии.
Локальные `.iwiki.toml` и `.iwikiignore` по-прежнему создаёт и меняет инициализация
проекта; hosted-сервер создаёт состояние домена в PostgreSQL и эти файлы не пишет.

## Время жизни сессии и происхождение биндинга

Выбор `wiki_bind` **живёт только в процессе сервера и только в рамках сессии**. Он
ключуется по `mcp-session-id`, истекает после 24 часов бездействия и не переживает
перезапуск сервера. Если выбор не найден, сервер откатывается к дефолтному скоупу
самого токена и продолжает отвечать — откат разрешён, но он больше не молчаливый:

- `wiki_status`, `wiki_bind`, `wiki_code_status`, `wiki_code_search`,
  `wiki_code_context`, `wiki_code_publish_begin`, `wiki_spec_search`,
  `wiki_spec_context` и `wiki_spec_resolve` несут `binding_source` со значением
  `session` (выбор сделан `wiki_bind` в этой сессии) или `token_default` (откат,
  построенный из grants токена).
- `tools/call`, отклонённый на authorization gate, несёт тот же `binding_source` в
  payload `access_denied`, поэтому отказ из-за потерянного выбора распознаётся без
  второго вызова.
- Чтения кодграфа без параметра домена при `token_default` дополнительно добавляют
  `binding_defaulted` в `warnings`, поэтому ответ по снапшоту чужого проекта распознаётся,
  даже когда он сообщает `state: ready` и `fresh: true`. `wiki_spec_search` и
  `wiki_search`, вызванные без `domains`, берут множество поиска из bound read list и
  сообщают то же предупреждение по той же причине; при явном `domains` оно не
  появляется. `wiki_search(intent="write")` предпочитает bound primary любому названному
  домену, поэтому при откате предупреждение приходит всегда.
- `wiki_bind` возвращает `session_id`, к которому привязался выбор, — так виден ответ,
  принадлежащий другой сессии.
- Если пересечение со scope записи заменило выбранный primary, ответ несёт
  `primary_substituted: true` и `requested_primary`.

Контракт клиента: пере-биндиться после переподключения, после простоя и всякий раз, когда
ответ сообщает `binding_source: token_default`. Hosted-сервер может превратить этот откат
в отказ для чтений кодграфа опцией `code_graph.require_session_binding = true`; тогда эти
три инструмента возвращают `{"error": "binding_not_selected"}` и никакого содержимого
снапшота, пока не выполнен `wiki_bind`. Опция выключена по умолчанию и не влияет на
Markdown-инструменты, которые называют домен явно.

```toml
[code_graph]
require_session_binding = false # true отказывает в чтениях кодграфа при откате
```

## Контракт MCP-инструментов PostgreSQL

| Поддержка PostgreSQL | Инструменты |
| --- | --- |
| Поддерживаются | `wiki_status`, `wiki_list_domains`, `wiki_list_pages`, `wiki_read_page`, `wiki_search`, `wiki_related`, `wiki_write_page`, `wiki_update_page`, `wiki_insert_section`, `wiki_delete_section`, `wiki_move_section`, `wiki_delete_page`, `wiki_index`, `wiki_bind`, `wiki_lint` |
| Только hosted PostgreSQL | `wiki_create_domain`, `wiki_list_domain_grants`, `wiki_set_domain_grant`, `wiki_revoke_domain_grant` |
| Поддерживается code graph | `wiki_code_status`, `wiki_code_search`, `wiki_code_context` |
| Только hosted PostgreSQL | `wiki_code_publish_begin`, `wiki_code_publish_batch`, `wiki_code_publish_finalize`, `wiki_code_publish_abort`, `wiki_code_refresh_links` |
| Только локальный checkout | `wiki_code_index` |
| Только Git | `wiki_remediation_plan`, `wiki_migrate_okf`, `wiki_apply_okf`, `wiki_export_okf`, `wiki_sync` |

Git-only инструменты возвращают
`{"error":"unsupported_storage","storage":"postgres","hint":"use this tool with Git storage"}`.
Три grant-инструмента вне hosted PostgreSQL возвращают `unsupported_transport` с
фактическими `storage` и `transport`. `wiki_create_domain(name)` требует
`can_create_domain`, атомарно создаёт домен, read/write grant creator-токена и строку
`can_manage_grants`, затем возвращает `created`, `already_existed`, `domain` и полный
effective scope сессии. Точный retry идемпотентен.

Bootstrap нового домена проекта выполняется в таком порядке: сначала bind только тех
доменов, которые уже существуют, затем `wiki_create_domain(name)` — он расширяет текущую
сессию новым доменом и делает его primary, — и только потом rebind полного scope проекта.
Bind scope, который называет ещё не созданный домен, отклоняется gate с
`reason: "domain_not_granted"`: gate сверяет запрошенный scope с текущими grants токена, а
несуществующий домен не даёт никаких grants. Этот reason отличает домен, который
вызывающий ещё может создать, от отозванного grant. Токен без `can_create_domain`
отклоняется с `reason: "domain_creation_not_allowed"`, а create-capable токен, назвавший
домен, которым уже владеет другой токен, получает in-band
`{"error":"access_denied","reason":"domain_not_owned"}`: `can_create_domain` создаёт новый
домен и никогда не присваивает существующий.

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
сопоставить отказ с id своего запроса. В этом `data` также приходит собственный
`binding_source` вызывающего и, когда gate может атрибутировать отказ, `reason`. Hint
остаётся намеренно неконкретным, и ни одно поле не называет домен, wiki или чужой токен.
Batch-запрос, отклонённый так же, остаётся на HTTP
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
