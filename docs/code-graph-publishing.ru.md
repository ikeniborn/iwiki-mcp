# Распределённая публикация code graph

*Часть [документации iwiki-mcp](README.ru.md#документация). English version: [code-graph-publishing.md](code-graph-publishing.md).*

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
superseded_retention_seconds = 86400
staging_cleanup_limit = 100
```

`read_mode` выбирает, откуда отвечают `wiki_code_status`, `wiki_code_search` и
`wiki_code_context` — ровно так же, как `publish_mode` выбирает, куда уходит собранный
снапшот. `sqlite` — значение по умолчанию — отвечает из локального кэша code graph и не
меняется: те же защищённые чтения, та же ограниченная авто-пересборка. `postgres` и
`mcp` отвечают из опубликованного снапшота: чтение не индексирует, не пересобирает и не
трогает исходники проекта, а свежесть — это то, что сообщает сам снапшот
(`missing_snapshot`, `stale_snapshot`). `wiki_code_index` остаётся локальной сборкой при
любом режиме чтения. Если предпосылка выбранного режима отсутствует — нет привязки к
хранилищу PostgreSQL для `postgres`, нет `IWIKI_CODE_GRAPH_MCP_URL` /
`IWIKI_CODE_GRAPH_MCP_TOKEN` для `mcp` — чтение возвращает `{"error": ..., "code":
"invalid_config", "field": "read_mode", "hint": ...}`, называя ключ, который нужно
исправить, и никогда не повторяется через другой режим. Хранилище Wiki на PostgreSQL
всегда читает из собственной базы, поэтому там `read_mode` уже нечего выбирать.

`superseded_retention_seconds` задаёт, сколько держится снапшот, переставший быть
активным, прежде чем он станет пригоден для очистки — и никогда активный. Вытесненный
снапшот не читает ничто: все запросы идут через
`code_graph_domain_state.active_snapshot_id`, поэтому окно существует только чтобы
оставить оператору цель для ручного возврата.

Очистка выполняется single-flight фоновым воркером, а не внутри публикации, поэтому
публикация никогда не задерживается и не падает из-за неё. Цикл вычерпывает накопившееся
по одному закоммиченному батчу за раз, где батч — это не более 10 000 строк из одной
дочерней таблицы одного снапшота, дети раньше родителя. Цикл держит одно соединение на всю
свою длительность и останавливается по достижении потолка строк за цикл или когда очередь
пуста. Если он остановился рано — или был убит — уже закоммиченные строки остаются
удалёнными, а следующий цикл продолжает с этого места, поскольку состояние хранится в базе,
а не в воркере. Каждый батч заново проверяет, что снапшот всё ещё вытеснен, поэтому
оператор, возвращающий его посреди слива, теряет не больше одного батча его строк, а не
весь снапшот.

Эту работу планируют две вещи, и ни одна из них её не выполняет. `begin` ставит в очередь
работу для того домена, который публикует; любой другой аутентифицированный hosted-запрос
может поставить по одной работе на каждый домен из `binding.write`, не чаще одного раза в
900 секунд на вики. Очередь разбирает фиксированный набор из двух обслуживающих воркеров,
поэтому рост числа клиентов даёт очередь, а не потоки и соединения. Очередь ограничена, и
постановка в переполненную отбрасывается со счётчиком — работа вернётся со следующим
запросом, потому что её никто не ждёт.

Один батч — это одно удаление не более 10 000 строк из одной дочерней таблицы одного
снапшота, и каждый батч коммитится отдельно. Поэтому убийство процесса стоит не больше
одного такого батча, а следующий цикл продолжает с того места, где остановился предыдущий.
Воркер держит одно соединение на весь цикл и берёт его из собственного пула обслуживания,
никогда — из того, который делят инструменты и аутентификация: всего соединений к
PostgreSQL у сервера `pool_max_size` плюс два обслуживающих воркера, двенадцать при
поставляемых значениях по умолчанию.

Прямая публикация через CLI в PostgreSQL — известный пробел здесь: процесс завершается
вскоре после того, как `finalize` вернул ответ, поэтому запланированный им цикл очистки
может быть убит до того, как он вычерпает очередь.

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

Ошибка `invalid_config` называет свою причину, когда доступен безопасный идентификатор:
ответ имеет вид `{"error": ..., "code": "invalid_config", "field": "<имя>", "hint":
...}`, где `field` — виновный ключ конфигурации или параметр запроса (например `depth`
или ключ `.iwiki.toml` с опечаткой). Имя проходит строгий идентификаторный фильтр,
поэтому текст исключений, значения и пути в него не попадают; при отсутствии безопасного
имени ключ просто отсутствует. Каждый не-ready ответ запроса также несёт `error`, `code`
и `hint` рядом с пустыми `results`, поэтому его нельзя спутать с пустым результатом
фильтра.

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
в primary-домен проекта; `wiki_bind` выбирает внутри уже выданного scope и не может выйти
за его пределы. Сессия принадлежит создавшей её личности: другой токен с правом записи в тот
же домен не может дополнить, прервать или завершить её, а процесс-замена обязан открыть
новую сессию.

`wiki_code_refresh_links(domain)` заново выводит `DOCUMENTED_BY`-ссылки активного
снапшота из текущего Markdown домена. Он не разбирает исходники и не резолвит символы,
поэтому снимает `wiki_links_stale` за время, пропорциональное числу страниц, а не
пересобирая граф. Сам снапшот не меняется: `snapshot_revision`, `graph_payload_revision`
и счётчики файлов, символов и связей после вызова те же, меняются только
`code_graph_wiki_links` и сохранённая Markdown-ревизия. В отличие от вызовов публикации
он принимает домен явно, потому что мутирует, а протухший биндинг сессии иначе увёл бы
его в другой primary; токен обязан иметь право записи в этот домен. Без активного
готового снапшота он отвечает `missing_snapshot`, а не запускает сборку.

Батчи несут только строки — никогда файл базы, текст исходников, абсолютный путь
checkout, учётные данные или сформированные издателем wiki-ссылки. Цель пересчитывает
ревизию полезной нагрузки, выводит ссылки code-to-wiki из собственного Markdown
назначения и активирует снапшот одним коммитом. Поэтому читатели видят либо предыдущую
полную ревизию, либо новую, но никогда частичную загрузку. Повтор принятого ordinal с
теми же строками идемпотентно успешен; повтор с другими строками возвращает
`batch_conflict`.

При активации PostgreSQL вставляет каждый тип строк графа и производные Wiki-ссылки
одним set-based запросом, совместимым с RLS. Pipelined `executemany` здесь недостаточно:
он всё равно исполняет отдельный `INSERT` для каждой строки, поэтому снапшот на 100 000
строк под нагрузкой базы может исчерпать deadline удалённого вызова, даже если сетевые
round-trip скрыты.

Повторяйте публикацию целиком после `busy`, `session_expired`, `snapshot_conflict`,
`revision_mismatch` или `markdown_unavailable`: откройте новую сессию и отправьте
заново. `snapshot_conflict` означает, что активный снапшот или Markdown назначения
изменились, пока сессия была открыта, поэтому перестроенный граф нужно публиковать
против текущего состояния. Истёкшие staging-сессии убираются ограниченными порциями при
открытии следующей сессии, синхронно и напрямую — это отдельный механизм от воркера
вытесненных снапшотов выше, который остаётся единственным фоновым демоном в этом пути.

Для чтений PostgreSQL или удалённого MCP `include_source=true` возвращает контекст графа
без исходников плюс `source_unavailable`; сервер никогда не запрашивает исходники у
издателя. Локальные чтения SQLite сохраняют существующее защищённое поведение с
локальными исходниками. Лимиты search и context применяются для каждого адаптера чтения,
поэтому удалённый вызывающий не может запросить неограниченный результат или неявно
загрузить весь граф.

Первая публикация в пустой домен — обычная сессия: status сообщает `missing_snapshot`,
пока первый `finalize` не завершится успешно.

## Плановая публикация оператором

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

Когда transport теряет ответ на `finalize` — timeout или разорванное соединение —
publisher спрашивает target, что произошло на самом деле, и только потом сообщает
результат. Уже терминальная сессия воспроизводит свой terminal result, поэтому
публикация, которую target довёл до конца, завершается с `0` как ready, а не с `1`.
Как publication failure сообщается только сессия, которую target не активировал.

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

## Профили снапшота SQLite и неопределённость коммита

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
