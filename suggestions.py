"""Suggestion engine: uses OpenAI to suggest Wikipedia articles for a photo."""

import json
import os

from openai import OpenAI

# Use the same key as other apps on this server
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")


def suggest_articles_for_photo(
    categories: list[str],
    description: str,
    current_articles: list[str],
) -> list[dict]:
    """Use GPT to suggest Wikipedia articles that could use this photo.

    Args:
        categories: Commons categories on the photo.
        description: Photo description text.
        current_articles: Articles already using this photo.

    Returns:
        List of {title, reason} dicts.
    """
    if not OPENAI_API_KEY:
        return [{"title": "Error", "reason": "OPENAI_API_KEY not set"}]

    client = OpenAI(api_key=OPENAI_API_KEY)

    cats_str = ", ".join(categories[:15]) if categories else "None"
    current_str = ", ".join(current_articles[:20]) if current_articles else "None"

    prompt = f"""You are a Wikipedia editor. A Wikimedia Commons photo has these details:

Description: {description or 'No description'}
Categories: {cats_str}
Currently used in these articles: {current_str}

Suggest 5-10 Wikipedia articles (English Wikipedia) where this photo could be added.
Focus on articles that:
- Are directly relevant to the photo subject
- Don't already use this photo
- Would genuinely benefit from this image
- Are real, existing Wikipedia articles

Return a JSON array of objects with "title" (exact Wikipedia article title) and "reason" (brief explanation why).
Return ONLY the JSON array, no other text."""

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=1000,
        )

        text = response.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        suggestions = json.loads(text)
        return suggestions[:10]
    except json.JSONDecodeError:
        return [{"title": "Parse error", "reason": "Could not parse AI response"}]
    except Exception as e:
        return [{"title": "Error", "reason": str(e)}]
