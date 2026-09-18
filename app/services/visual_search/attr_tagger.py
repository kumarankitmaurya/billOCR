"""Optional colour/fabric/border/work tags for faceted filtering.

Off unless VISUAL_ATTR_TAGGING_ENABLED is set. It plays no part in what
matches — that's the embedding's job — it only lets a query narrow results
to, say, `color=red`. A failure here never fails indexing: the photo is
stored with attrs NULL and the caller is told it wasn't tagged.
"""

import base64
import json
import logging

from app.config import settings
from app.services.visual_search.repo import FACETS

logger = logging.getLogger(__name__)

_PROMPT = (
    "This is a photo of a saree. Reply with only a JSON object with these keys, "
    "each a short lowercase phrase or null if you can't tell: "
    '"color" (dominant body colour), "fabric" (e.g. silk, georgette, cotton), '
    '"border" (e.g. zari, plain, contrast), "work" (e.g. embroidery, print, weave).'
)


def _parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    data = json.loads(text)
    return {
        key: str(data[key]).strip().lower()
        for key in FACETS
        if isinstance(data, dict) and data.get(key) not in (None, "")
    }


def _gemini(jpeg: bytes) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), _PROMPT],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return response.text


def _groq(jpeg: bytes) -> str:
    from groq import Groq

    client = Groq(api_key=settings.groq_api_key)
    data_uri = f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"
    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": _PROMPT},
                ],
            }
        ],
        temperature=0.1,
        max_tokens=256,
    )
    return response.choices[0].message.content


def tag(jpeg: bytes) -> dict | None:
    """Return {color, fabric, border, work} (any subset), or None on failure."""
    callers = []
    if settings.gemini_api_key:
        callers.append(_gemini)
    if settings.groq_api_key:
        callers.append(_groq)

    for call in callers:
        try:
            return _parse(call(jpeg)) or None
        except Exception as exc:
            logger.warning("attribute tagging via %s failed: %s", call.__name__, exc)
    return None
