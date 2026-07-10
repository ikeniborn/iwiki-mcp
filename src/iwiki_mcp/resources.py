"""Page-authoring rules, exposed as an MCP resource the agent fetches before
writing. Ported from the iwiki-ingest skill's section-formation rules.
"""

AUTHORING_RULES: str = """\
# iwiki page authoring rules

- Use **only `##`** for sections -- never `###` or deeper. Deeper headings are not
  indexed as separate units; flatten them into the `##` section's prose.
- Put **no content before the first `##`** except the frontmatter block and a
  single `# Title` H1.
- Lead with `# Title`, then the page's `##` sections directly. Do NOT write a
  `## Overview` section -- the article summary is the frontmatter `description`.
- One `##` section per concept; lead each section with a <=250-char paragraph
  stating what it covers and why it matters (intent, not just mechanics).
- Prefer a standard section name where one fits: `## Purpose`, `## Interface`,
  `## API`, `## Dependencies`, `## Data flow`, `## Errors`, `## Usage`.
- Wrap every code symbol (function, path, flag, command, config key) in backticks.
- Cross-link related pages with `[Heading](slug.md#heading)` (within the same domain in v1).
- Write accurate English prose grounded in the real source; do not invent.

## OKF frontmatter

- Every page carries a YAML frontmatter block above the `# Title` H1. The write
  tools fill it. Fields: `type` (required), `title`, `description`, `resource`,
  `tags`, `status`, `timestamp`.
- `description` is the authored article summary and the single source of it (it is
  embedded as each section's context prefix). Write it rich: include `Covers:` and
  `Terms:` keyword lines so retrieval matches the page. There is no `## Overview`.
- `type` is an OPEN vocabulary. Prefer a common value -- `architecture`, `api`,
  `guide`, `reference`, `runbook`, `concept` (default) -- but any lower-case value
  is allowed (e.g. `person`, `team`); an off-list value is only advised, not rejected.
- `status` is one of `stub` (default), `developing`, `stable`, `deprecated`.
- `tags` are lowercase kebab-case, <=5 per page; reuse an existing domain tag first.
- Put relationship links in two reserved sections, `## Outgoing links` (Markdown links
  to other pages) and `## External links` (bare URLs). Both are EXCLUDED from search
  indexing but still feed the link graph (`wiki_related`, `lint`).
- The slugs `index` and `log` are reserved: `index.md` / `log.md` are generated
  OKF navigation/history files kept fresh on every write. The write tools reject them.
"""
