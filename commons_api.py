"""Wikimedia Commons API client for fetching user uploads, file usage, and metadata."""

import httpx

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "PhotoPost/1.0 (https://github.com/photopost; photopost@example.com)"
BATCH_SIZE = 50


def _get(client: httpx.Client, params: dict) -> dict:
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    params.setdefault("maxlag", "5")
    resp = client.get(API_URL, params=params)
    resp.raise_for_status()
    return resp.json()


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
    )


def get_user_uploads(client: httpx.Client, username: str, limit: int = 200) -> list[dict]:
    """Fetch files uploaded by a Commons user. Returns list of image info dicts."""
    uploads = []
    params = {
        "action": "query",
        "list": "allimages",
        "aisort": "timestamp",
        "aidir": "descending",
        "aiuser": username,
        "ailimit": min(limit, BATCH_SIZE),
        "aiprop": "timestamp|url|size|mime|extmetadata",
    }

    while len(uploads) < limit:
        data = _get(client, params)
        images = data.get("query", {}).get("allimages", [])
        if not images:
            break
        uploads.extend(images)
        cont = data.get("continue")
        if not cont:
            break
        params["aicontinue"] = cont["aicontinue"]

    return uploads[:limit]


def get_file_details(client: httpx.Client, filenames: list[str]) -> dict:
    """Fetch global usage, categories, and metadata for a batch of files.

    Args:
        filenames: List of filenames (with or without 'File:' prefix).

    Returns:
        Dict keyed by filename with keys: global_usage, categories, imageinfo.
    """
    # Ensure File: prefix
    titled = [f if f.startswith("File:") else f"File:{f}" for f in filenames]

    results = {}
    # Process in batches of 50 (API limit)
    for i in range(0, len(titled), BATCH_SIZE):
        batch = titled[i : i + BATCH_SIZE]
        params = {
            "action": "query",
            "titles": "|".join(batch),
            "prop": "globalusage|fileusage|categories|imageinfo",
            "iiprop": "extmetadata|url|size|mime",
            "gulimit": "500",
            "fulimit": "500",
            "cllimit": "500",
        }

        data = _get(client, params)
        pages = data.get("query", {}).get("pages", [])

        for page in pages:
            title = page.get("title", "")

            # globalusage = usage on other wikis (en.wikipedia.org, etc.)
            global_usage = page.get("globalusage", [])

            # fileusage = usage on Commons itself (articles, galleries, etc.)
            file_usage = [
                {
                    "title": fu.get("title", ""),
                    "wiki": "commons.wikimedia.org",
                    "url": f"https://commons.wikimedia.org/wiki/{fu.get('title', '').replace(' ', '_')}",
                }
                for fu in page.get("fileusage", [])
            ]

            results[title] = {
                "global_usage": global_usage + file_usage,
                "categories": [c["title"] for c in page.get("categories", [])],
                "imageinfo": page.get("imageinfo", [{}])[0] if page.get("imageinfo") else {},
            }

    return results


def get_image_description(imageinfo: dict) -> str:
    """Extract a plain-text description from imageinfo extmetadata."""
    ext = imageinfo.get("extmetadata", {})
    desc = ext.get("ImageDescription", {}).get("value", "")
    # Strip HTML tags roughly
    import re
    return re.sub(r"<[^>]+>", "", desc).strip()


def get_image_categories_clean(categories: list[str]) -> list[str]:
    """Remove 'Category:' prefix and filter out maintenance categories."""
    skip_prefixes = (
        "CC-", "GFDL", "Self-published", "Uploaded with", "Files from",
        "Media needing", "Pages with", "All free", "License migration",
    )
    cleaned = []
    for cat in categories:
        name = cat.removeprefix("Category:").strip()
        if not any(name.startswith(p) for p in skip_prefixes):
            cleaned.append(name)
    return cleaned
