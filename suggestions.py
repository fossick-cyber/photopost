"""Suggestion engine: uses OpenAI to suggest Wikipedia articles across all languages,
then validates each suggestion exists and generates ready-to-paste wikicode."""

import json
import os
import time

import httpx
from openai import OpenAI

USER_AGENT = "PhotoPost/1.0 (https://github.com/fossick-cyber/photopost)"


def _get_api_key():
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    return key


def _verify_articles_multi(articles: list[dict]) -> list[dict]:
    """Verify articles exist across multiple wikis. Each article is {title, wiki, ...}.
    Returns only articles that exist, with canonical titles."""
    if not articles:
        return []

    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15)
    verified = []

    # Group by wiki
    from collections import defaultdict
    by_wiki = defaultdict(list)
    for a in articles:
        by_wiki[a.get("wiki", "en.wikipedia.org")].append(a)

    for wiki, items in by_wiki.items():
        api_url = f"https://{wiki}/w/api.php"

        # Batch in groups of 50
        for i in range(0, len(items), 50):
            batch = items[i:i + 50]
            titles = "|".join(a["title"] for a in batch)

            try:
                time.sleep(0.3)
                resp = client.get(api_url, params={
                    "action": "query",
                    "titles": titles,
                    "format": "json",
                    "formatversion": "2",
                    "maxlag": "5",
                })
                if resp.status_code != 200:
                    continue
                data = resp.json()
                existing = {}
                for page in data.get("query", {}).get("pages", []):
                    if not page.get("missing") and not page.get("invalid"):
                        existing[page["title"]] = page["title"]

                # Also build normalized lookup
                for norm in data.get("query", {}).get("normalized", []):
                    if norm.get("to") in existing:
                        existing[norm["from"]] = existing[norm["to"]]

                for a in batch:
                    canonical = (
                        existing.get(a["title"])
                        or existing.get(a["title"].replace("_", " "))
                        or existing.get(a["title"].replace(" ", "_"))
                    )
                    if canonical:
                        a["title"] = canonical
                        verified.append(a)
            except Exception:
                continue

    return verified


def suggest_articles_for_photo(
    categories: list[str],
    description: str,
    current_articles: list[str],
    filename: str = "",
    current_usages: list[dict] = None,
) -> list[dict]:
    """Use GPT to suggest Wikipedia articles in all languages, verify they exist,
    and generate ready-to-paste wikicode.

    Args:
        categories: Commons categories on the photo.
        description: Photo description text.
        current_articles: Article titles already using this photo (for exclusion).
        filename: The Commons filename.
        current_usages: List of {article_title, wiki} dicts showing current placements.
    """
    api_key = _get_api_key()
    if not api_key:
        return [{"title": "Error", "reason": "OPENAI_API_KEY not set"}]

    client = OpenAI(api_key=api_key)

    cats_str = ", ".join(categories[:15]) if categories else "None"

    # Build a rich context of current usage
    usage_lines = []
    if current_usages:
        for u in current_usages[:30]:
            wiki = u.get("wiki", "")
            title = u.get("article_title", "")
            lang = wiki.replace(".wikipedia.org", "") if wiki else "?"
            usage_lines.append(f"  - {lang}: {title}")
    usage_str = "\n".join(usage_lines) if usage_lines else "  None"

    prompt = f"""You are an experienced Wikipedia editor. A Wikimedia Commons photo needs to be placed on more Wikipedia articles.

Photo filename: {filename or 'Unknown'}
Description: {description or 'No description'}
Categories: {cats_str}

The photo is ALREADY used on these articles (do NOT suggest these):
{usage_str}

Based on the articles it's already on and the photo's subject, suggest 15-20 Wikipedia articles where this photo should also be added. Include articles in MULTIPLE LANGUAGES — especially major languages like Spanish, French, German, Portuguese, Russian, Japanese, Chinese, Italian, Dutch, Polish, and any others relevant to the subject.

For each suggestion provide:
- "title": exact article title in that language's Wikipedia
- "wiki": the wiki domain (e.g. "es.wikipedia.org", "de.wikipedia.org", "en.wikipedia.org")
- "lang": language code (e.g. "es", "de", "en")
- "reason": 1 sentence in English explaining why
- "description": a short image caption/description in THAT language (for the wikicode)

Return ONLY a JSON array. No other text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=3000,
        )

        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        suggestions = json.loads(text)

        # Ensure all have wiki field
        for s in suggestions:
            if "wiki" not in s or not s["wiki"]:
                s["wiki"] = "en.wikipedia.org"
            if "lang" not in s:
                s["lang"] = s["wiki"].replace(".wikipedia.org", "")

        # Filter out articles already in current usages
        current_set = set()
        if current_usages:
            for u in current_usages:
                current_set.add((u.get("wiki", ""), u.get("article_title", "")))
                current_set.add((u.get("wiki", ""), u.get("article_title", "").replace(" ", "_")))

        filtered = [
            s for s in suggestions
            if (s["wiki"], s["title"]) not in current_set
            and (s["wiki"], s["title"].replace("_", " ")) not in current_set
        ]

        # Verify all exist
        verified = _verify_articles_multi(filtered)

        # Generate wikicode for each
        safe_filename = filename.replace("_", " ") if filename else "FILENAME.jpg"
        for s in verified:
            desc = s.get("description", "")
            s["wikicode"] = f"[[File:{safe_filename}|thumb|{desc}]]"

        return verified[:15]

    except json.JSONDecodeError:
        return [{"title": "Parse error", "reason": "Could not parse AI response"}]
    except Exception as e:
        return [{"title": "Error", "reason": str(e)}]
