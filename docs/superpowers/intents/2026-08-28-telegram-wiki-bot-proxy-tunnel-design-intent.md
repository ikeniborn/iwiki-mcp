---
review:
  intent_hash: 818a467c15f8253b
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

Enable the existing Telegram wiki bot to operate from a server that has no direct access to Telegram by routing Telegram Bot API traffic through an operator-owned proxy or tunnel endpoint on a VPS.

## Desired Outcomes

- The bot starts without direct network access to Telegram.
- The bot receives Telegram updates through long polling over the configured proxy.
- The bot sends Telegram replies over the configured proxy.
- The bot downloads Telegram voice files over the configured proxy.

## Health Metrics

- Secrets are never written to logs.
- Telegram content is not persisted by the bot or proxy integration.
- iwiki and inference traffic continue to use their existing direct network paths.
- Page creation and section-update confirmation remain bound to the requesting Telegram user and retain their current expiry, replay, and conflict protections.

## Strategic Context

- Interacts with: the Telegram wiki bot, Telegram Bot API, an operator-owned VPS proxy endpoint, hosted iwiki, the inference endpoint, systemd, and the deployment operator.
- Priority trade-off: reliability and trust.

## Constraints

### Steering (behavioral guidance)

- Compare efficient tunnel implementations by reliability, trust boundary, long-polling behavior, operational overhead, and recovery after connection failure.
- Treat the proxy or tunnel process as an explicit deployment dependency of the Telegram bot.
- Prefer the smallest deployment design that satisfies the current Telegram connectivity requirement.

### Hard (architectural enforcement)

- Accept only a complete SOCKS or HTTPS proxy URL, including its custom port, as the bot-facing proxy configuration contract.
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
- Escalate if: proxy protocol support requires changing the allowed SOCKS/HTTPS contract, the recommended deployment needs production network changes beyond the operator-owned VPS and bot host, or reliable recovery cannot be demonstrated.
- Done when: the checked design defines the full SOCKS/HTTPS proxy URL contract with custom ports, makes the tunnel an explicit deployment dependency, compares viable options, recommends one with evidence, specifies failure and recovery behavior, and defines acceptance checks for startup, long polling, replies, and voice-file downloads without direct Telegram access.
