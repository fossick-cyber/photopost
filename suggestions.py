"""Suggestion engine: uses OpenAI to suggest Wikipedia articles for a photo,
then validates each suggestion actually exists on Wikipedia."""

import json
import os

import httpx
from openai import OpenAI

def _get_api_key():
    """Read API key at call time, not import time."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        # Fallback: read from .env file directly
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    return key
WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "PhotoPost/1.0 (https://github.com/fossick-cyber/photopost)"


def _verify_articles(titles: list[str]) -> dict[str, str]:
    """Check which article titles exist on Wikipedia. Returns {title: canonical_title}
    for articles that exist. Handles up to 50 titles per batch."""
    if not titles:
        return {}

    verified = {}
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15)

    # Batch in groups of 50
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        resp = client.get(WIKI_API, params={
            "action": "query",
            "titles": "|".join(batch),
            "format": "json",
            "formatversion": "2",
        })
        data = resp.json()
        for page in data.get("query", {}).get("pages", []):
            if not page.get("missing") and not page.get("invalid"):
                # Map both the original title and canonical title
                verified[page["title"]] = page["title"]

    return verified


def suggest_articles_for_photo(
    categories: list[str],
    description: str,
    current_articles: list[str],
) -> list[dict]:
    """Use GPT to suggest Wikipedia articles, then verify they exist."""
    api_key = _get_api_key()
    if not api_key:
        return [{"title": "Error", "reason": "OPENAI_API_KEY not set"}]

    client = OpenAI(api_key=api_key)

    cats_str = ", ".join(categories[:15]) if categories else "None"
    current_str = ", ".join(current_articles[:20]) if current_articles else "None"

    prompt = f"""You are a Wikipedia editor. A Wikimedia Commons photo has these details:

Description: {description or 'No description'}
Categories: {cats_str}
Currently used in these articles: {current_str}

Suggest 10-15 English Wikipedia articles where this photo could be added.
Focus on articles that:
- Are directly relevant to the photo's subject matter
- Don't already use this photo (see list above)
- Are well-known, established Wikipedia articles (not stubs or obscure pages)
- Would genuinely benefit from this specific image

Use exact Wikipedia article titles (case-sensitive, with underscores replaced by spaces).

Return a JSON array of objects with "title" (exact Wikipedia article title) and "reason" (1 sentence why).
Return ONLY the JSON array, no other text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1500,
        )

        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        suggestions = json.loads(text)

        # Verify all titles exist on Wikipedia
        # Send both underscore and space versions to maximize matches
        titles = []
        for s in suggestions:
            t = s.get("title", "")
            if t:
                titles.append(t)
                if "_" in t:
                    titles.append(t.replace("_", " "))
        existing = _verify_articles(titles)

        verified = []
        for s in suggestions:
            title = s.get("title", "")
            # Normalize underscores to spaces for matching
            normalized = title.replace("_", " ")
            if title in existing:
                s["title"] = existing[title]
                verified.append(s)
            elif normalized in existing:
                s["title"] = existing[normalized]
                verified.append(s)

        return verified[:10]

    except json.JSONDecodeError:
        return [{"title": "Parse error", "reason": "Could not parse AI response"}]
    except Exception as e:
        return [{"title": "Error", "reason": str(e)}]
