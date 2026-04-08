"""Suggestion engine: finds Wikipedia articles that could use a photo but don't yet."""

import httpx

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "PhotoPost/1.0 (https://github.com/photopost; photopost@example.com)"


def _wiki_get(client: httpx.Client, params: dict) -> dict:
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    resp = client.get(WIKIPEDIA_API, params=params, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.json()


def search_articles(client: httpx.Client, query: str, limit: int = 10) -> list[dict]:
    """Search Wikipedia for articles matching a query string.

    Returns list of {title, snippet, pageid}.
    """
    data = _wiki_get(client, {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": limit,
        "srprop": "snippet",
    })
    return data.get("query", {}).get("search", [])


def get_article_images(client: httpx.Client, title: str) -> list[str]:
    """Get list of images used in a Wikipedia article."""
    data = _wiki_get(client, {
        "action": "query",
        "titles": title,
        "prop": "images",
        "imlimit": "500",
    })
    pages = data.get("query", {}).get("pages", [])
    if not pages:
        return []
    return [img["title"] for img in pages[0].get("images", [])]


def suggest_articles_for_photo(
    client: httpx.Client,
    categories: list[str],
    description: str,
    current_articles: set[str],
) -> list[dict]:
    """Suggest Wikipedia articles where a photo could be added.

    Uses the photo's categories and description to search for relevant articles,
    then filters out articles that already use the photo.

    Returns list of {title, snippet, reason}.
    """
    suggestions = []
    seen_titles = set()

    # Build search queries from categories (most specific signal)
    queries = []
    for cat in categories[:5]:  # Top 5 categories
        queries.append(cat)

    # Also try the description if it's meaningful
    if description and len(description) > 10:
        # Take first 100 chars as a search query
        queries.append(description[:100])

    for query in queries:
        results = search_articles(client, query, limit=5)
        for result in results:
            title = result["title"]
            if title in seen_titles or title in current_articles:
                continue
            seen_titles.add(title)
            suggestions.append({
                "title": title,
                "snippet": result.get("snippet", ""),
                "reason": f"Matches category/description: {query}",
            })

    return suggestions[:10]  # Cap at 10 suggestions per photo
