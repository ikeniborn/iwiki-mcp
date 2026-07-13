---
review:
  spec_hash: 7fc46f0be6c81129
  last_run: 2026-07-13
  phases:
    structure: { status: passed }
    coverage: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
  findings: []
chain:
  intent: docs/superpowers/intents/2026-07-13-embedding-startup-check-intent.md
---
# Embedding Endpoint Startup Check Design

**Date:** 2026-07-13
**Status:** approved
**Intent:** `docs/superpowers/intents/2026-07-13-embedding-startup-check-intent.md`

## 1. Purpose and Scope

`iwiki-mcp` currently starts its stdio MCP transport without proving that the
configured embeddings service is usable. Missing configuration or endpoint
failure becomes visible only when an embedding-dependent tool runs. This design
adds a hard preflight gate: the process validates configuration and performs one
minimal embeddings request before `mcp.run()` starts the protocol.

The change covers startup validation, safe diagnostics, automated tests,
repository documentation, iwiki documentation, and the required patch version
bump. It does not add a degraded mode, a health-check MCP tool, a new CLI option,
or a new environment variable.

## 2. Requirements

### R1. Startup Order

After argument parsing and application of `--project`, `main()` must load the
embedding configuration and run the endpoint probe before calling `mcp.run()`.
`mcp.run()` must be called exactly once after a successful probe and never after
a configuration or probe failure. `--help` remains handled by `argparse` and
exits without loading configuration or contacting the endpoint.

Acceptance criterion: a unit test proves the ordered success path, failure tests
prove that `mcp.run()` is not called, and a CLI check proves that `--help` does
not require embedding configuration or network access.

### R2. Configuration Validation

`Config.load()` remains the environment boundary. It must continue to require
non-empty `IWIKI_LLM_BASE_URL` and `IWIKI_LLM_KEY`, reject an explicitly empty
embedding model, and require `IWIKI_EMBED_DIMENSIONS` to parse as a positive
integer. Configuration failures must raise `ConfigError` with the relevant
environment variable name and must never include the key value. Existing valid
defaults and provider-neutral configuration remain unchanged.

Acceptance criterion: focused tests cover missing URL, missing key, empty model,
non-integer dimensions, and non-positive dimensions, and each failure is a
`ConfigError` with actionable variable names and no secret value.

### R3. Dedicated Endpoint Probe

Add `probe_embedding_endpoint(cfg)` to `engine/embed.py`. It posts one request to
`{cfg.base_url}/embeddings` with input `"iwiki startup probe"`, the configured
model, and configured dimensions. The request timeout is 10 seconds and the
probe performs no retry. It uses the same Bearer authentication and
OpenAI-compatible request shape as normal embedding calls.

The probe does not call or alter `embed_texts()`, return a reusable vector, write
state, or populate a cache. This keeps normal indexing and retrieval retry
behavior unchanged.

Acceptance criterion: a fake HTTP client proves the exact URL, payload, header,
timeout, single attempt, and lack of side effects.

### R4. Probe Response Validation

A successful probe requires a successful HTTP status and valid JSON containing
exactly one embedding result for the one probe input. Its `embedding` value must
be a non-empty list of finite numbers, excluding booleans, and its length must
equal `cfg.dimensions`. Any HTTP error, transport error, timeout, malformed JSON,
missing data, extra or missing results, invalid vector value, or dimension
mismatch raises `EmbedError` with a concise safe reason.

Acceptance criterion: focused tests cover one valid response and every listed
failure class without external network access.

### R5. Startup Failure Diagnostic

`main()` catches only expected `ConfigError` and startup-probe `EmbedError`
failures. It writes a prominent multi-line diagnostic to stderr, writes nothing
to stdout, and exits with status code `1` without a traceback. The diagnostic
contains:

- `iwiki-mcp: startup failed`;
- the full resolved embeddings URL, or `<not set>` when unavailable;
- the resolved model, or `<not set>` when unavailable;
- a concise failure reason;
- a hint naming `IWIKI_LLM_BASE_URL`, `IWIKI_LLM_KEY`,
  `IWIKI_EMBED_MODEL`, and `IWIKI_EMBED_DIMENSIONS`.

The diagnostic must not contain the API key, authorization header, response
body, or a serialized `Config`. Unexpected programming errors remain uncaught by
this startup handler so normal traceback-based diagnosis is preserved.

Acceptance criterion: unit tests assert exit status, stderr fields, empty stdout,
absence of a known fake secret and response body, and propagation of an
unexpected exception.

### R6. Existing Runtime Behavior

`embed_texts()` retains its current public signature, 60-second request timeout,
three-attempt transient retry policy, payload, response ordering, and
`EmbedError` behavior. MCP tool-level `_safe` error handling remains unchanged
for failures that occur after startup.

Acceptance criterion: existing embedding retry/error tests continue to pass
without weakened assertions.

### R7. MCP Smoke Coverage

The subprocess MCP smoke test must no longer depend on inherited credentials or
an external endpoint. Its harness starts a deterministic loopback HTTP stub that
accepts the startup probe and returns a vector matching the configured test
dimensions. The stub records the request so the test can assert exactly one
startup probe. The spawned server receives explicit test-only embedding env
values, passes the real startup gate, initializes MCP, lists tools, and calls
`wiki_status`.

Acceptance criterion: the smoke test passes without external network access and
would fail if the server skipped the startup probe or could not initialize after
it.

### R8. Documentation and Version

Update `README.md` and `docs/README.ru.md` to state that startup performs one
embedding request, blocks on failure, reports details on stderr, uses a
10-second no-retry probe, and leaves `--help` available offline. Bump the package
patch version from `0.6.9` to `0.6.10` and update lock metadata.

After functionality changes, update the bound iwiki pages `mcp-server`,
`indexing`, and `installation` through iwiki MCP write tools. Run `wiki_lint` and
record any pre-existing advisory findings separately from regressions.

Acceptance criterion: repository docs and iwiki describe the implemented
behavior, package and lock versions agree, `wiki_lint` reports no new broken or
stale pages, and version consistency tests pass.

## 3. Component Design

### 3.1 `engine/config.py`

This module owns environment parsing and local validation only. It converts
invalid embedding model/dimension configuration into safe `ConfigError`
messages. It performs no network I/O and does not format the final startup
diagnostic.

### 3.2 `engine/embed.py`

This module owns the OpenAI-compatible probe request and response validation.
The probe is a separate function so its one-attempt, 10-second startup policy
cannot change the runtime policy of `embed_texts()`. Small request-shape
duplication is accepted to keep the change isolated.

### 3.3 `server.py`

This module owns startup orchestration and the user-facing stderr diagnostic.
It applies `--project`, loads `Config`, calls the probe, and only then starts
FastMCP. It does not interpret response JSON or implement network retries.

### 3.4 Tests and Documentation

Engine tests prove configuration and protocol validation. Server tests prove
ordering, process behavior, and secret-safe output. The subprocess smoke test
proves integration with the actual stdio MCP launch. Repository and iwiki docs
explain the externally visible contract.

## 4. Data Flow

1. The MCP client spawns `iwiki-mcp` with environment configuration.
2. `argparse` handles CLI arguments; `--help` exits here.
3. `main()` applies `--project` to `IWIKI_PROJECT_DIR`.
4. `Config.load()` returns a locally valid configuration or raises
   `ConfigError`.
5. `probe_embedding_endpoint(cfg)` sends one request and either returns `None`
   after validating the vector or raises `EmbedError`.
6. On success, `main()` calls `mcp.run()` and stdio MCP traffic begins.
7. On an expected failure, `main()` emits the safe stderr diagnostic and exits
   `1` before any protocol output.

## 5. Error Model

Configuration errors identify the invalid or absent environment variable.
Endpoint errors distinguish HTTP status, connection/transport failure, timeout,
invalid JSON/shape, invalid vector values, and dimension mismatch. Error text is
constructed from controlled labels, status code/reason phrase, and the allowed
full endpoint. It never incorporates response bodies, request headers, or
configuration object representations.

A transient startup failure is deliberately fatal after one attempt. Operators
restart the MCP client after correcting configuration or endpoint availability.
Normal embedding work retains retries because it occurs after a trusted startup.

## 6. Testing Strategy

All unit HTTP behavior is replaced with deterministic fakes. The only socket used
by automated coverage is the loopback stub in the subprocess smoke test; no test
contacts an external service. In the inherited acceptance wording, "without real
network access" means without an external network route or service; the local
loopback stub is controlled test infrastructure. Focused tests run first,
followed by:

```bash
uv run pytest -q
uv run flake8 src tests
uv run iwiki-mcp --help
git diff --check
```

The successful startup outcome is observed through MCP initialization and tool
listing. The failure outcome is observed through a blocked `mcp.run()`, process
exit status `1`, empty stdout, and the required stderr fields.

## 7. Risks and Mitigations

- A brief endpoint outage blocks startup. This is intentional and follows the
  approved trust-over-availability priority; the 10-second single attempt bounds
  delay and avoids hiding the failure behind retries.
- A provider may accept embeddings but return an unusable shape. Full vector and
  dimension validation prevents a false healthy result.
- Startup diagnostics could leak secrets through generic exception text.
  Controlled error construction excludes headers, response bodies, keys, and
  serialized configuration.
- The existing subprocess smoke test could accidentally depend on developer
  credentials. Explicit loopback configuration makes it deterministic.

## 8. Acceptance (from intent)

### Desired Outcomes

- When the configured embeddings endpoint is unavailable, the MCP server
  refuses to start and emits a prominent, understandable notification.
- When the endpoint is available, the server starts normally and all existing
  wiki capabilities remain available.

### Done when

Done when an available endpoint permits normal server startup, an unavailable
endpoint prevents startup with the required stderr diagnostic, existing
embedding behavior remains compatible, and automated tests prove both startup
paths without real network access.
