"""Optional server-side page classification via an OpenAI-compatible chat
endpoint. Fills the governed ``type``/``tags`` frontmatter when IWIKI_CHAT_MODEL
is configured. Any failure degrades to the default type with no tags and a
warning — classification must never fail a write.
"""
from __future__ import annotations
import json
import httpx
from .config import Config
from . import frontmatter as fm

_TIMEOUT = 60.0
_PROMPT = """You classify a documentation page.

Return ONLY compact JSON: {{"type": "<one-of>", "tags": ["...", ...]}}.

type MUST be exactly one of: {types}.
Pick by the dominant intent:
- architecture: system structure, components, data flow, modules
- api: a call/interface surface — functions, endpoints, signatures
- guide: how to do something — step-by-step, usage
- reference: lookup material — tables of keys, flags, configs
- runbook: operational procedure — deploy, incident steps
- concept: explains an idea/model (default when unsure)

tags: up to {max_tags} short lowercase topic tags. PREFER reusing an existing
tag from this list when one fits; only coin a new tag if none match:
{existing}

PAGE:
{body}
"""


def _chat(cfg: Config, prompt: str) -> str:
    url = f"{cfg.base_url}/chat/completions"
    payload = {"model": cfg.chat_model,
               "messages": [{"role": "user", "content": prompt}],
               "temperature": 0}
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    resp = httpx.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def classify_page(cfg: Config, body: str, existing_tags: list) -> dict:
    prompt = _PROMPT.format(
        types=", ".join(fm.OKF_TYPES), max_tags=fm.MAX_TAGS,
        existing=", ".join(existing_tags) or "(none yet)", body=body[:6000],
    )
    try:
        raw = _chat(cfg, prompt)
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        return {"type": fm.coerce_type(data.get("type")),
                "tags": fm.normalize_tags(data.get("tags", []) or []),
                "warning": None}
    except Exception as e:
        return {"type": fm.DEFAULT_TYPE, "tags": [],
                "warning": f"classification unavailable: {e}"}
