---
review:
  intent_hash: eba71cdb331b847f
  last_run: 2026-08-28
  phases:
    structure: { status: passed }
    completeness: { status: passed }
    clarity: { status: passed }
    consistency: { status: passed }
    alignment: { status: passed }
  findings: []
workflow:
  route: chain
  continuation: full
---
# Intent: telegram-wiki-bot-proxy-tunnel-design

**Date:** 2026-08-28
**Status:** approved

## Objective

Deliver one runtime container for the hosted iwiki MCP server, Telegram wiki bot, LAN/Traefik reverse proxy, and Telegram HTTPS proxy integration so the bot can operate from a server that has no direct access to Telegram while PostgreSQL remains an external operator-managed service.

## Desired Outcomes

- An operator starts one container that runs the hosted iwiki MCP server, Telegram bot, LAN/Traefik reverse proxy, and Telegram HTTPS proxy integration without deploying PostgreSQL from this project.
- The hosted iwiki MCP server connects to the separately managed PostgreSQL service and remains available to the Telegram bot and authorized MCP clients.
- The single container replaces the current separate `iwiki-mcp-proxy-1` deployment while preserving its LAN/Traefik ingress behavior.
- The bot starts without direct network access to Telegram.
- The bot receives Telegram updates through long polling over the configured proxy.
- The bot sends Telegram replies over the configured proxy.
- The bot downloads Telegram voice files over the configured proxy.

## Health Metrics

- Secrets are never written to logs.
- Telegram content is not persisted by the bot or proxy integration.
- iwiki and inference traffic continue to use their existing direct network paths.
- Hosted iwiki authentication, tenant and domain isolation, and PostgreSQL durability semantics do not change.
- Page creation and section-update confirmation remain bound to the requesting Telegram user and retain their current expiry, replay, and conflict protections.

## Strategic Context

- Interacts with: the hosted iwiki MCP server, Telegram wiki bot, Telegram Bot API, an operator-owned VPS HTTPS proxy or tunnel endpoint, external PostgreSQL, the inference endpoint, the in-container nginx reverse proxy, Traefik when used, authorized MCP clients, Docker, and the deployment operator.
- Priority trade-off: reliability and trust.

## Constraints

### Steering (behavioral guidance)

- Compare efficient tunnel implementations by reliability, trust boundary, long-polling behavior, operational overhead, and recovery after connection failure.
- Treat Telegram HTTPS proxy reachability as an explicit startup and health dependency of the Telegram bot.
- Run the hosted iwiki MCP server, Telegram bot, LAN/Traefik reverse proxy, and any required local tunnel process under explicit supervision in one runtime container.
- Prefer the smallest process and network design that satisfies the current hosted MCP, LAN/Traefik ingress, and Telegram connectivity requirements.

### Hard (architectural enforcement)

- Require a complete HTTPS proxy URL beginning with `https://`, including its custom port, as the bot-facing proxy configuration contract.
- Include one container build and deployment definition for the hosted iwiki MCP server, Telegram bot, LAN/Traefik reverse proxy, and Telegram HTTPS proxy integration.
- Do not include a PostgreSQL service or image in the deployment and do not make the runtime container own database creation or bootstrap; preserve the existing operator-managed PostgreSQL administration, migration, and schema-validation contract.
- Preserve the current MCP ingress topology inside the single container: the hosted server listens on `127.0.0.1:8765`, nginx exposes the configured LAN/Traefik address and port (production example `192.168.68.123:8766`), and all paths are forwarded to the loopback upstream.
- Preserve the current nginx ingress contract: client `Authorization` reaches the hosted MCP server, request bodies up to 16 MiB are accepted, access logging is disabled, and the nginx configuration is immutable at runtime.
- Keep the nginx MCP ingress distinct from the outbound Telegram HTTPS forward proxy; nginx must not become a proxy for Telegram, inference, or other outbound traffic.
- Define one container-level healthcheck that fails when any required supervised process or the local MCP ingress path is unhealthy, and retain restart policy `unless-stopped` in the deployment definition.
- Route only Telegram Bot API requests, update polling, and Telegram file downloads through the proxy.
- Keep hosted iwiki and inference requests on their existing direct paths.
- Do not log proxy credentials, Telegram credentials, or Telegram content.
- Do not persist Telegram updates, messages, replies, voice files, or transcriptions as part of proxy support.
- Preserve the current preview, confirmation-token, expiry, replay, user-binding, and write-conflict rules.
- The remote proxy or tunnel endpoint is owned and operated on the user's VPS.

## Autonomy Zones

- Full autonomy (reversible, low risk): inspect the current solution, research tunnel options, compare them, and draft the architecture and deployment design.
- Guarded (log + confidence threshold): recommend one option only when its security, reliability, protocol support, and operational behavior are supported by primary-source evidence.
- Proposal-first (needs approval): modify application code, deployment files, dependencies, or the public environment-variable contract.
- No autonomy (human only): configure the user's VPS, firewall, DNS, proxy credentials, Telegram credentials, or production services.

> These zones OVERRIDE subagent-driven-development's "continuous execution, don't pause" default. Any task touching proposal-first or no-go decisions is a HUMAN CHECKPOINT.

## Stop Rules

- Halt if: an option requires logging or persisting credentials or Telegram content, cannot support Telegram long polling and file downloads, or cannot keep iwiki and inference traffic outside the proxy path.
- Escalate if: literal HTTPS proxy support requires an unsupported or unmaintained client transport, the recommended deployment needs production network changes beyond the operator-owned VPS and container host, the hosted server cannot retain its current external PostgreSQL contract, or reliable recovery cannot be demonstrated.
- Done when: the checked design defines the literal HTTPS proxy URL contract with custom ports; specifies one supervised runtime container containing the hosted MCP server, Telegram bot, LAN/Traefik nginx ingress, and any required local tunnel process without PostgreSQL; preserves the current `127.0.0.1:8765` to LAN/Traefik reverse-proxy contract; compares viable HTTPS tunnel options; recommends one with evidence; specifies process, proxy, and container failure recovery; and defines acceptance checks for hosted MCP ingress plus Telegram startup, long polling, replies, and voice-file downloads without direct Telegram access.
