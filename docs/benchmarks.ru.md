# Бенчмарки

*Часть [документации iwiki-mcp](README.ru.md#документация). English version: [benchmarks.md](benchmarks.md).*

Бенчмарки только для оценки. Ни один из них не меняет production-поведение поиска, веса fusion или настройки rerank.

## Бенчмарк типов страниц System One

Пилот типов страниц сравнивает текущий чат-классификатор с System One shadow на
проверенном оператором JSONL-корпусе. Храните корпус вне version control и обезличьте
его перед использованием. Каждая строка содержит только нужные бенчмарку данные:

```json
{"id":"reviewed-001","label":"guide","body":"# Reviewed page\n\n## Steps\n..."}
```

Настройте `IWIKI_CHAT_MODEL`, включите `IWIKI_SYSTEM1_SHADOW` и передайте отдельные
`IWIKI_SYSTEM1_BASE_URL` (корень API, оканчивающийся на `/v1`) и
`IWIKI_SYSTEM1_KEY` (и при необходимости `IWIKI_SYSTEM1_MODEL`, например `laya-iwiki`,
чтобы оценить конкретный алиас) через окружение процесса. Затем запустите:

```bash
uv run python -m eval.system1_page_type --corpus /path/to/reviewed-pages.jsonl --output /tmp/system1-page-type-report.json
```

Runner сначала полностью измеряет baseline и фиксирует его p95 latency до любого
запроса System One. Aggregate-only отчёт содержит accuracy, macro-F1, p50/p95, а также
Brier score System One и ECE по 10 интервалам. Рекомендация `go` выдаётся, если качество
не хуже и System One укладывается в зафиксированный p95; `fine-tune` — если latency
проходит, но качество падает; `reject` — если p95 ухудшается. Любое unavailable или
invalid решение останавливает прогон без оценки частичных evidence. Команда не хранит
тела страниц и идентификаторы кейсов в отчёте.

## Code graph benchmark

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
uv run python -m eval.search_pipeline --domain iwiki-mcp --out replace-with-report-dir --pareto --replay-evidence replace-with-evidence.json
```

Только после успешного replay запросите подтверждение оператора перед live benchmark. Live-команда использует созданный оператором environment file; не читайте и не копируйте его credentials в репозиторий:

```bash
uv run python -m eval.search_pipeline --domain iwiki-mcp --out replace-with-report-dir --modes hybrid,lexical,semantic --pareto --env-file replace-with-operator-env-file
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
