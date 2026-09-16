# Подготовка PostgreSQL и минимальные привилегии

*Часть [документации iwiki-mcp](README.ru.md#документация). English version: [postgres-setup.md](postgres-setup.md).*

Оператор создаёт БД и устанавливает расширение `vector`. Отдельный только
административный владелец схемы/migrator через admin-команды репозитория создаёт и
мигрирует только схему `iwiki` до запуска runtime. Никогда не настраивайте эти
credentials как login работающего сервера. Runtime-role получает после миграции только
`CONNECT`, `USAGE` и нужные права на таблицы и sequences; он не владеет схемой, не
получает `CREATE` и не запускает миграции. Не выдавайте доступ к посторонним схемам.
Вне изолированного dev-хоста используйте `sslmode="verify-full"`, доверенный CA и
совпадающее имя хоста БД.

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
iwiki-mcp token list --iwiki team-wiki
iwiki-mcp token set-create-domain --iwiki team-wiki --token-id replace-with-token-id --enabled
iwiki-mcp token set-domain-management --iwiki team-wiki --token-id replace-with-token-id --domain backend --enabled
iwiki-mcp token revoke --token-id replace-with-token-id
iwiki-mcp base import-git --iwiki team-wiki --path /srv/old-wiki --dry-run --json
iwiki-mcp base export-git --iwiki team-wiki --path /srv/rollback-wiki --dry-run --json
```

`token create` показывает plaintext-токен один раз. Поэтому примеры ниже выводят его в
терминал: не запускайте их в записываемой сессии и передавайте результат прямо в secret
manager. Для production используйте процедуру захвата без вывода из
[deployment runbook](deployment.md#out-of-band-schema-migration-and-principal-provisioning).
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

## Runtime-принципалы для code graph

Три роли базы данных остаются раздельными. Владелец схемы и мигратор — только
административные учётные данные: он владеет схемой `iwiki` и применяет миграции через
admin-команды, и он никогда не настраивается как логин работающего сервера. Hosted
service principal — роль, под которой подключается hosted-сервер. Direct runtime
principal — роль локального индексера в прямом режиме PostgreSQL. Обе runtime-роли не
являются владельцем, не имеют `BYPASSRLS`, не выполняют миграции и не получают `CREATE`
на базу или схему. Row-level security включается обычным
`ENABLE ROW LEVEL SECURITY`, никогда `FORCE`, поскольку владелец — административная роль.

Сначала примените миграции через только административную конфигурацию, затем создайте
base и домены. Каждая не dry-run admin-команда, кроме пути совместимости схемы, до своей
основной операции проверяет и продвигает схему; deployment-runbook использует `base
list` как явный операторский триггер миграции.

```bash
iwiki-mcp base list --config /opt/iwiki-mcp/admin-server.toml --json
iwiki-mcp base create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki
iwiki-mcp domain create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --domain backend
```

До регистрации создайте PostgreSQL login runtime-роли вне iwiki. Его пароль и runtime-
конфигурация остаются отдельно от конфигурации владельца схемы. `principal grant` не
создаёт роль и не принимает её пароль. Явно зарегистрируйте каждую runtime-роль и её
доменные гранты, затем проверьте точную hosted-роль до выпуска любого токена.

```bash
iwiki-mcp principal grant --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --principal iwiki_hosted --runtime hosted --read-domain backend --write-domain backend
iwiki-mcp principal grant --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --principal iwiki_indexer --runtime direct --read-domain backend --write-domain backend
iwiki-mcp principal inspect --config /opt/iwiki-mcp/admin-server.toml --principal iwiki_hosted --json
```

Только после этой проверки выпускайте токены против точной развёрнутой hosted-роли.
`token create` требует `--hosted-principal ROLE`, где `ROLE` равен `[storage].user`
hosted-сервера. Команда проверяет, что именно эта роль зарегистрирована как
`runtime=hosted`, не является владельцем, не имеет `BYPASSRLS` и уже покрывает каждый
запрошенный домен чтения и записи до генерации любого материала токена. Это относится и
к bootstrap-токену с `--can-create-domain`; другая hosted-роль или общая проверка
«какая-то hosted-роль существует» заменой не является.

```bash
iwiki-mcp token create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --owner deploy --hosted-principal iwiki_hosted --read-domain backend --write-domain backend
iwiki-mcp token create --config /opt/iwiki-mcp/admin-server.toml --iwiki team-wiki --owner bootstrap --hosted-principal iwiki_hosted --read-domain backend --write-domain backend --can-create-domain
iwiki-mcp serve --transport streamable-http
```

Старт выполняет одинаковую проверку схемы для hosted HTTP и stdio: сервер сверяет точную
ожидаемую версию схемы и собственный подключённый `session_user` с выданными грантами и
иначе отказывается стартовать. Миграции неявно не выполняются никогда.

## Откат схемы v5 и артефакт совместимости

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
