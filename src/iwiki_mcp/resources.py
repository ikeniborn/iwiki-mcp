"""Page-authoring rules, exposed as an MCP resource the agent fetches before
writing. Ported from the iwiki-ingest skill's section-formation rules.
"""

AUTHORING_RULES: str = """\
# iwiki page authoring rules

- Use **only `##`** for sections -- never `###` or deeper. Deeper headings are not
  indexed as separate units; flatten them into the `##` section's prose.
- Put **no content before the first `##`** except the frontmatter block and a
  single `# Title` H1.
- Lead with `# Title`, then a first `## Overview` section summarizing all of the
  page's sections in <=400 characters. The Overview is NOT indexed as its own
  section; it gives every other section whole-article context.
- One `##` section per concept; lead each section with a <=250-char paragraph
  stating what it covers and why it matters (intent, not just mechanics).
- Prefer a standard section name where one fits: `## Purpose`, `## Interface`,
  `## API`, `## Dependencies`, `## Data flow`, `## Errors`, `## Usage`.
- Wrap every code symbol (function, path, flag, command, config key) in backticks.
- Cross-link related pages with `[Heading](slug.md#heading)` (within the same domain in v1).
- Write accurate English prose grounded in the real source; do not invent.

## OKF frontmatter

- Every page carries a YAML frontmatter block above the `# Title` H1. The write
  tools fill it; you rarely hand-author it. Fields: `type` (required), `title`,
  `description`, `resource`, `tags`, `timestamp`.
- `type` MUST be one of the closed vocabulary -- `architecture`, `api`, `guide`,
  `reference`, `runbook`, `concept` (default). Pick by dominant intent:
  `architecture` = structure/data flow; `api` = call surface; `guide` = how-to;
  `reference` = lookup tables; `runbook` = ops procedure; `concept` = an idea/model.
- `tags` are lowercase kebab-case, <=5 per page; reuse an existing domain tag
  before coining a new one.
"""
