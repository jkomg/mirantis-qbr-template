"""
review.py — optional AI color commentary on a QBR payload.

Uses OPENAI_API_KEY or ANTHROPIC_API_KEY from the environment.
Stdlib only (urllib) so the sidecar image stays lean.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def review_available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))


def _compact_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Send a slim snapshot — enough for status commentary, not the full deck."""
    customer = payload.get("customer") or {}
    commercial = payload.get("commercial") or {}
    arr = commercial.get("arr") or {}
    usage = payload.get("usage") or {}
    support = payload.get("support") or {}
    nps = payload.get("nps") or {}
    meta = payload.get("_meta") or {}

    def trim_list(items: Any, keys: List[str], limit: int = 5) -> List[Dict[str, Any]]:
        if not isinstance(items, list):
            return []
        out = []
        for row in items[:limit]:
            if not isinstance(row, dict):
                continue
            out.append({k: row.get(k) for k in keys if k in row})
        return out

    return {
        "customer": {
            "name": customer.get("name"),
            "tier": customer.get("tier"),
            "industry": customer.get("industry"),
            "stakeholders": (customer.get("stakeholders") or [])[:6],
        },
        "quarter": payload.get("quarter"),
        "commercial": {
            "arrCurrent": arr.get("current"),
            "arrPrior": arr.get("prior"),
            "yoyPct": arr.get("yoyPct"),
            "pipelineUSD": commercial.get("pipelineUSD"),
            "renewalDate": commercial.get("renewalDate"),
            "renewalSponsor": commercial.get("renewalSponsor"),
            "expansions": trim_list(commercial.get("expansions"), ["name", "valueUSD", "quarter", "stage"], 6),
        },
        "usage": {
            "environments": usage.get("environments"),
            "clusters": usage.get("clusters"),
            "nodes": usage.get("nodes"),
            "workloads": usage.get("workloads"),
            "uptime": usage.get("uptime"),
        },
        "support": {
            "ticketsTotal": support.get("ticketsTotal"),
            "p1Count": support.get("p1Count"),
            "p1MttrHours": support.get("p1MttrHours"),
            "slaMetPct": support.get("slaMetPct"),
            "csat": support.get("csat"),
            "openCases": support.get("_openCases"),
        },
        "nps": nps,
        "products": payload.get("products") or [],
        "productMix": trim_list(
            payload.get("productMix"),
            ["product", "entitlement", "inUse", "utilizationPct", "trend"],
            8,
        ),
        "incidents": trim_list(
            payload.get("incidents"),
            ["date", "severity", "summary", "duration", "rcaStatus"],
            5,
        ),
        "risks": trim_list(payload.get("risks"), ["level", "title", "body", "action"], 5),
        "wins": trim_list(payload.get("wins"), ["kind", "headline", "body"], 5),
        "execSummaryTakeaways": trim_list(
            payload.get("execSummaryTakeaways"),
            ["kind", "label", "headline", "body"],
            4,
        ),
        "nextActions": trim_list(
            payload.get("nextActions"),
            ["commitment", "owner", "dueDate", "status", "kind"],
            6,
        ),
        "sfCounts": meta.get("counts") or {},
        "sfWarnings": meta.get("warnings") or [],
    }


SYSTEM_PROMPT = """You are a Mirantis Technical Account Manager coach.
Given Salesforce-derived QBR account data, write concise color commentary for an internal prep review.
Be specific to the numbers provided. Do not invent ARR, NPS, wins, or customer quotes that are not in the data.
If a field is 0, empty, or missing, say so and treat it as a gap for the TAM — do not fabricate.
Tone: direct, executive-ready, Mirantis-aware (MKE, MOSK, k0rdent, Lens, OpsCare).
Return ONLY valid JSON matching this schema:
{
  "statusLabel": "Healthy | Watch | At risk | Expanding | Thin data",
  "commentary": "2-4 sentences of status color commentary",
  "strengths": ["short bullet", "..."],
  "watchItems": ["short bullet", "..."],
  "suggestedAsks": ["short ask for the next QBR", "..."],
  "suggestedTakeaways": [
    {"kind": "momentum|watch|decide", "label": "01 · FOOTPRINT", "headline": "...", "body": "..."}
  ],
  "confidence": "high|medium|low"
}
Provide 2-4 strengths, 2-4 watch items, 2-3 suggested asks, and 3 suggestedTakeaways when data allows.
"""


def _http_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int = 60) -> Dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"LLM HTTP {e.code}: {detail}") from e


def _extract_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("Model did not return JSON")
    return json.loads(text[start : end + 1])


def _call_openai(context: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    body = {
        "model": model,
        "temperature": 0.3,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Account QBR snapshot:\n" + json.dumps(context, indent=2, default=str),
            },
        ],
    }
    raw = _http_json(
        "https://api.openai.com/v1/chat/completions",
        {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        body,
    )
    content = raw["choices"][0]["message"]["content"]
    return _extract_json_object(content), f"openai/{model}"


def _call_anthropic(context: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    body = {
        "model": model,
        "max_tokens": 1200,
        "temperature": 0.3,
        "system": SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": "Account QBR snapshot:\n" + json.dumps(context, indent=2, default=str),
            }
        ],
    }
    raw = _http_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body,
    )
    parts = raw.get("content") or []
    text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
    return _extract_json_object(text), f"anthropic/{model}"


def generate_review(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not review_available():
        raise RuntimeError(
            "AI review is not configured. Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env "
            "and rebuild/restart sf-sync."
        )
    context = _compact_context(payload or {})
    if os.environ.get("OPENAI_API_KEY"):
        parsed, provider = _call_openai(context)
    else:
        parsed, provider = _call_anthropic(context)

    strengths = parsed.get("strengths") if isinstance(parsed.get("strengths"), list) else []
    watch = parsed.get("watchItems") if isinstance(parsed.get("watchItems"), list) else []
    asks = parsed.get("suggestedAsks") if isinstance(parsed.get("suggestedAsks"), list) else []
    takeaways = parsed.get("suggestedTakeaways") if isinstance(parsed.get("suggestedTakeaways"), list) else []

    return {
        "statusLabel": parsed.get("statusLabel") or "Watch",
        "commentary": parsed.get("commentary") or "",
        "strengths": [str(x) for x in strengths][:6],
        "watchItems": [str(x) for x in watch][:6],
        "suggestedAsks": [str(x) for x in asks][:5],
        "suggestedTakeaways": takeaways[:4],
        "confidence": parsed.get("confidence") or "medium",
        "provider": provider,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
    }
