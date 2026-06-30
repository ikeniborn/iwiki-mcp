"""Page-authoring rules, exposed as an MCP resource the agent fetches before
writing. Ported from the iwiki-ingest skill's section-formation rules.
"""

AUTHORING_RULES: str = """\
# iwiki page authoring rules

- Use **only `##`** for sections -- never `###` or deeper. Deeper headings are not
  indexed as separate units; flatten them into the `##` section's prose.
- Put **no content before the first `##`** except a single `# Title` H1.
- Lead with `# Title`, then a first `## Overview` section summarizing all of the
  page's sections in <=400 characters. The Overview is NOT indexed as its own
  section; it gives every other section whole-article context.
- One `##` section per concept; lead each section with a <=250-char paragraph
  stating what it covers and why it matters (intent, not just mechanics).
- Prefer a standard section name where one fits: `## Purpose`, `## Interface`,
  `## API`, `## Dependencies`, `## Data flow`, `## Errors`, `## Usage`.
- Wrap every code symbol (function, path, flag, command, config key) in backticks.
- Cross-link related pages with `[[slug#Heading]]` (within the same domain in v1).
- Write accurate English prose grounded in the real source; do not invent.
"""
