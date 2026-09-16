# Given-When-Then спецификации

*Часть [документации iwiki-mcp](README.ru.md#документация). English version: [specifications.md](specifications.md).*

Обычные Wiki-страницы и явные страницы `type: specification` сосуществуют в каждом
домене. Во всех режимах обычные Wiki-страницы сохраняют прежние запись, индексацию,
поиск и lint и не требуют code graph.

Сценарий допускается в проекцию именно типом страницы: разбирается только страница,
чей `type` нормализуется в `specification`. Fence `iwiki-gwt` в любом другом месте
хранится как обычный Markdown и никогда не попадает в проекцию, поиск и resolution,
поэтому `wiki_write_page` и `wiki_update_page` возвращают `warning`, когда сценарий
оказывается на такой странице, а `wiki_lint` сообщает advisory-находку секции
`unprojected_scenario`. Ни то, ни другое не блокирует запись — пример в документации,
цитирующий fence, легитимен, — но язык fence является маркером намерения, поэтому
оформляйте иллюстративные примеры как ```` ```toml ````.

Локальные Git и PostgreSQL читают политику проекта из `.iwiki.toml`:

```toml
[specifications]
mode = "optional"
```

Hosted PostgreSQL читает операторскую политику из server TOML:

```toml
[specifications]
default_mode = "optional"
allow_project_mode = true

[[specifications.overrides]]
iwiki_id = "team-wiki"
domain = "payments"
mode = "strict"
```

Локальная политика действует для всех видимых доменов. Hosted-клиент передаёт значение
проекта через опциональный `wiki_bind.specification_mode`. Hosted precedence: точный
override `(iwiki_id, domain)`, проектный режим, hosted default, затем встроенный
`optional`. Проектный tier применяется только когда он не слабее hosted default и
`allow_project_mode` равен `true`; иначе `wiki_status` возвращает
`project_mode_suppressed: true`. Значение ограничено сессией и разрешается отдельно для
каждого bound domain. `disabled` хранит specification
как обычный Markdown и отключает semantic-инструменты. `optional` хранит Markdown,
выдаёт advisory-находки и проецирует только валидные, полные, уникальные сценарии.
`strict` отклоняет невалидное изменение целевой specification до изменения страницы
или projection.

Каждый сценарий — один fenced TOML-блок внутри своей H2-секции:

```iwiki-gwt
id = "confirm-account-opening"
title = "Confirm account opening"
given = [{ role = "event", name = "AccountOpeningRequested" }]
when = { role = "command", name = "ConfirmAccountOpening" }
then = [{ role = "event", name = "AccountOpened" }]
code = [
  { relation = "implements", phase = "when", symbol = "accounts.Account.confirm" },
  { relation = "verifies", symbol = "tests.accounts.test_confirm_account_opening" }
]
```

Роли `given`: `event`, `state`, `fact`; роли `when`: `command`, `request`, `action`;
роли `then`: `event`, `response`, `outcome` или эксклюзивный `exception`. Полный
сценарий имеет минимум по одной связи `implements` и `verifies`. Его ID остаётся
стабильным при изменении формулировок или расположения без изменения наблюдаемого
поведения.

Грамматика закрытая и ограниченная. `id` обязателен, занимает 1–128 байт UTF-8 и
соответствует `[a-z0-9]+(?:-[a-z0-9]+)*`. `title` обязателен и не может быть пустым,
не содержит NUL и ограничен 250 Unicode code points. `name` каждого элемента фазы
обязателен и не может быть пустым, не содержит NUL и ограничен 1 024 байт UTF-8.
Неизвестные ключи верхнего уровня, элементов фаз и bindings невалидны; синтаксически
некорректный TOML и повторяющиеся TOML-ключи также невалидны.

`given` обязателен и содержит 0 или больше элементов. `when` обязателен и содержит
ровно один элемент. `then` обязателен и содержит 1 или больше элементов. `code`
обязателен и содержит 1 или больше bindings. В каждом binding `phase` необязателен,
а selector представлен ровно одним из `symbol`, `file` или `source_glob`. Для полноты
нужны минимум по одному binding `implements` и `verifies`.

Грамматика binding точная: relation принимает строго `implements | verifies`, а
`phase` необязателен и принимает строго `given | when | then`. Значение selector —
непустая UTF-8 строка размером не более 4 096 байт без NUL. `symbol` — строка qualified
name для code graph; parser применяет к ней только общие scalar-ограничения selector,
не вводя более строгий regex. `file` и `source_glob` — безопасные относительные
POSIX-пути или шаблоны без внешних пробелов и с не более 256 сегментов пути. Они
запрещают обратную косую черту, абсолютный путь, префикс диска Windows, пустой сегмент,
`.` или `..`. `file` запрещает glob-метасимволы `*`, `?` и `[`, а `source_glob`
разрешает их. `code` ограничен 256 bindings. Повторяющаяся идентичность фазы
`(phase, role, name)` невалидна. Повторяющаяся идентичность binding
`(relation, phase, selector kind, selector)` невалидна.

Матрица режимов распространяется и на уже существующий невалидный контент. Режим
`disabled` не создаёт projection и specification-находок для отсутствующих, невалидных,
повторяющихся или неполных specification-сценариев. В режиме `optional` все
specification-находки advisory. В `strict` syntax-находки (`missing_scenario` и
`invalid_scenario`), `duplicate_scenario_id` и `incomplete_bindings` блокируют только
будущие мутации указанной явной specification-страницы. Projection- и
resolution-находки остаются advisory; обычные Wiki-страницы не затрагиваются ни в одном
режиме.

Точная semantic tool surface состоит из трёх инструментов:

- `wiki_spec_search` ищет проецированные сценарии внутри read scope вызывающего.
- `wiki_spec_context` читает сценарий, сохранённое evidence и freshness без resolve
  или мутации.
- `wiki_spec_resolve` разрешает объявленные bindings и сохраняет очищенное evidence;
  ему нужен write scope, и он не расширяет binding или code-publication authority.
  В hosted-режиме его `domain` обязан совпадать с bound primary. Отказ называет, какое
  именно собственное отношение биндинга ответило — `invalid_domain`,
  `primary_not_selected`, `primary_not_writable` или `not_bound_primary`, — поэтому
  сценарий, лежащий вне bound primary, не принимается за отсутствующий grant.

`wiki_status` показывает effective mode и projection state. `wiki_lint` добавляет
независимые specification-находки, не скрывая обычные Wiki-находки. Git хранит
канонический Markdown и отслеживаемый, пересобираемый `<domain>/specifications.jsonl`;
PostgreSQL транзакционно хранит ту же логическую projection под tenant/domain
authorization. В обоих backend каноническим остаётся Markdown.

Specification-запись `wiki_status` содержит `domain`, `mode`, `source`,
`projection_state`, `scenarios` и `bindings`, а также
`project_mode_suppressed: true`, если server switch отключил полученный проектный режим
или tighten-only guard его отклонил. Значения source:
`project | hosted_default | hosted_override | built_in_default`; значения projection
state: `disabled | absent | ready | stale | failed`. Полный перечень типов находок
`wiki_lint`: `missing_scenario`, `invalid_scenario`, `duplicate_scenario_id`,
`incomplete_bindings`, `projection_stale`, `projection_failed`, `binding_unresolved`,
`binding_ambiguous`, `resolution_not_checked`, `resolution_stale_spec`,
`resolution_stale_graph` и `graph_unavailable`. Lint остаётся read-only и всегда
возвращает полный обычный Wiki-отчёт.

Resolution evidence принимает `resolved`, `ambiguous`, `unresolved` или
`graph_unavailable`. Context вычисляет `fresh`, `stale_spec` или `stale_graph` по
текущим revision спецификации и графа. Если граф отсутствует или непригоден, сохраняйте
selectors и продолжайте repository search и executable tests; обычная Wiki-работа не
должна ждать восстановления графа. Отдельно записывайте test command, exit status и
repository revision: binding `verifies` указывает тест, но не запускает его.

Детерминированное измерение путей без timing gate запускается командой:

```bash
uv run pytest -q -m measurement tests/measurement/test_specification_paths.py -s
```
