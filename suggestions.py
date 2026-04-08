"""Suggestion engine: uses OpenAI to suggest Wikipedia articles across all languages.
Skips Wikipedia verification — just filters against known usages in our DB."""

import json
import os
import time
from collections import defaultdict

import httpx
from openai import OpenAI

USER_AGENT = "PhotoPost/1.0 (https://github.com/fossick-cyber/photopost)"


def _verify_and_resolve(suggestions: list[dict]) -> list[dict]:
    """Batch-verify suggestions exist on their target wikis.
    Resolves redirects to actual article titles. Returns only verified ones."""
    if not suggestions:
        return []

    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15)
    verified = []

    # Group by wiki for batching
    by_wiki = defaultdict(list)
    for s in suggestions:
        by_wiki[s.get("wiki", "en.wikipedia.org")].append(s)

    for wiki, items in by_wiki.items():
        api_url = f"https://{wiki}/w/api.php"

        for i in range(0, len(items), 50):
            batch = items[i:i + 50]
            titles = "|".join(s["title"] for s in batch)

            try:
                time.sleep(0.3)
                resp = client.get(api_url, params={
                    "action": "query",
                    "titles": titles,
                    "redirects": "1",  # resolve redirects
                    "format": "json",
                    "formatversion": "2",
                    "maxlag": "5",
                })
                if resp.status_code != 200:
                    continue

                data = resp.json()
                query = data.get("query", {})

                # Build redirect map: from -> to
                redirect_map = {}
                for r in query.get("redirects", []):
                    redirect_map[r["from"]] = r["to"]

                # Build normalization map
                norm_map = {}
                for n in query.get("normalized", []):
                    norm_map[n["from"]] = n["to"]

                # Build set of existing pages
                existing_pages = set()
                for page in query.get("pages", []):
                    if not page.get("missing"):
                        existing_pages.add(page["title"])

                for s in batch:
                    title = s["title"]
                    # Follow normalization
                    resolved = norm_map.get(title, title)
                    # Follow redirect
                    resolved = redirect_map.get(resolved, resolved)

                    if resolved in existing_pages:
                        s["title"] = resolved  # Use canonical/redirect target
                        if resolved != title.replace("_", " "):
                            s["redirected_from"] = title
                        verified.append(s)

            except Exception:
                continue

    return verified


def _get_api_key():
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.startswith("OPENAI_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    return key


def generate_suggestions(
    categories: list[str],
    description: str,
    filename: str,
    current_usages: list[dict],
    existing_suggestions: list[str] = None,
    count: int = 10,
) -> list[dict]:
    """Ask GPT for article suggestions. Returns raw suggestions without Wikipedia verification.

    Args:
        categories: Commons categories on the photo.
        description: Photo description text.
        filename: The Commons filename.
        current_usages: List of {article_title, wiki} dicts showing current placements.
        existing_suggestions: Titles already suggested (to avoid repeats).
        count: How many new suggestions to generate.

    Returns:
        List of {title, wiki, lang, reason, description, wikicode} dicts.
    """
    api_key = _get_api_key()
    if not api_key:
        return [{"title": "Error", "reason": "OPENAI_API_KEY not set", "wiki": "", "lang": ""}]

    client = OpenAI(api_key=api_key)

    cats_str = ", ".join(categories[:15]) if categories else "None"

    # Current usage context
    usage_lines = []
    if current_usages:
        for u in current_usages[:30]:
            wiki = u.get("wiki", "")
            title = u.get("article_title", "")
            lang = wiki.replace(".wikipedia.org", "") if wiki else "?"
            usage_lines.append(f"  - {lang}: {title}")
    usage_str = "\n".join(usage_lines) if usage_lines else "  None"

    # Already suggested (to avoid repeats)
    exclude_str = ""
    if existing_suggestions:
        exclude_str = f"\n\nDo NOT suggest any of these (already suggested):\n  " + "\n  ".join(existing_suggestions[:50])

    prompt = f"""You are an experienced Wikipedia editor. A Wikimedia Commons photo needs to be placed on more Wikipedia articles.

Photo filename: {filename or 'Unknown'}
Description: {description or 'No description'}
Categories: {cats_str}

The photo is ALREADY used on these articles (do NOT suggest these):
{usage_str}
{exclude_str}

Suggest {count + 5} Wikipedia articles where this photo should be added. Suggest more than asked since some may not exist. Include articles in MULTIPLE LANGUAGES — especially major languages like Spanish, French, German, Portuguese, Russian, Italian, Dutch, Polish, and others relevant to the subject.

For each suggestion provide:
- "title": exact article title in that language's Wikipedia
- "wiki": the wiki domain (e.g. "es.wikipedia.org", "de.wikipedia.org")
- "lang": language code (e.g. "es", "de", "en")
- "reason": 1 sentence in English explaining why
- "description": a short image caption in THAT language (for wikicode)

Return ONLY a JSON array of {count} objects. No other text."""

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

        # Normalize and add wikicode
        safe_filename = filename.replace("_", " ") if filename else "FILENAME.jpg"
        for s in suggestions:
            if "wiki" not in s or not s["wiki"]:
                s["wiki"] = "en.wikipedia.org"
            if "lang" not in s:
                s["lang"] = s["wiki"].replace(".wikipedia.org", "")
            desc = s.get("description", "")
            s["wikicode"] = f"[[File:{safe_filename}|thumb|{desc}]]"

        # Filter out articles already in current usages
        current_set = set()
        if current_usages:
            for u in current_usages:
                current_set.add((u.get("wiki", ""), u.get("article_title", "").lower()))
                current_set.add((u.get("wiki", ""), u.get("article_title", "").replace(" ", "_").lower()))

        # Filter out already suggested
        existing_set = set(t.lower() for t in (existing_suggestions or []))

        filtered = []
        for s in suggestions:
            key = (s["wiki"], s["title"].lower())
            key2 = (s["wiki"], s["title"].replace("_", " ").lower())
            if key not in current_set and key2 not in current_set and s["title"].lower() not in existing_set:
                filtered.append(s)

        # Verify articles exist and resolve redirects
        verified = _verify_and_resolve(filtered)

        return verified[:count]

    except json.JSONDecodeError:
        return [{"title": "Parse error", "reason": "Could not parse AI response", "wiki": "", "lang": ""}]
    except Exception as e:
        return [{"title": "Error", "reason": str(e), "wiki": "", "lang": ""}]
