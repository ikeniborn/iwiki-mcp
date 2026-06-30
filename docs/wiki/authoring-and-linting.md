# Authoring rules and linting

## Overview
Three concerns keep pages well-formed and discoverable: the authoring rules agents must follow, the structural validator that enforces them, and the health linter that audits a whole domain. `resources.py` serves the rules, `engine/validate.py` checks one page, `engine/lint.py` checks all pages, and `engine/links.py` parses the `[[refs]]` both rely on. The validator's blocking rules gate [[mcp-server#Write path]].

## Authoring rules
`AUTHORING_RULES` (the `iwiki://authoring-rules` resource) states the section format the indexer assumes: use only `##` headings — never `###` or deeper; put no content before the first `##` except one `# Title`; lead with `# Title` then a `## Overview` summarizing all sections in ≤400 chars (not indexed itself); one `##` per concept, each opening with a ≤250-char lead. Wrap code symbols in backticks; cross-link with `[[slug#Heading]]`; write accurate English. These mirror [[indexing#Markdown chunking]].

## Section validation
`validate_page(content)` returns deterministic, API-free findings. Two are blocking: `deep_heading` (a `###`+ heading) and `pre_h2_text` (indexable text before the first `##`). Three are advisory: `missing_overview` (first section is not `Overview`), `missing_lead` (a section with no lead paragraph), and `long_lead` (a lead over `LEAD_MAX` 250). The blocking subset is what [[mcp-server#Write path]] refuses to persist.

## Wiki-link parsing
`parse_links(content)` extracts the target of every double-bracket wiki-link, including the alias form, de-duplicated and order-preserving. It first strips fenced and inline code spans so bracket syntax written inside a code example is never mistaken for a real link. Both [[retrieval#Related sections]] and the linter consume it.

## Health linting
`lint(wiki_dir)` reports domain health with stdlib only — no embedding call — so it runs in any project. An absent or empty wiki returns `{"wiki_present": false}` rather than erroring. Otherwise it reports `broken` links (missing target file or missing `#heading`), `orphans` (pages with no inbound reference), `stale` pages (source changed since the logged `src_hash`/mtime, via [[indexing#Ingest log]]), and per-page `sections` findings from the validator.
