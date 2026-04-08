"""Wikimedia Commons API client for fetching user uploads, file usage, and metadata."""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx

API_URL = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "PhotoPost/1.0 (https://github.com/fossick-cyber/photopost)"
BATCH_SIZE = 50
MAX_WORKERS = 6  # Parallel API requests (stay courteous)


def _params_defaults(params: dict) -> dict:
    params.setdefault("format", "json")
    params.setdefault("formatversion", "2")
    params.setdefault("maxlag", "5")
    return params


def _get(client: httpx.Client, params: dict) -> dict:
    resp = client.get(API_URL, params=_params_defaults(params))
    resp.raise_for_status()
    return resp.json()


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=30.0,
    )


def get_user_uploads(
    client: httpx.Client,
    username: str,
    limit: int = 10000,
    since: str | None = None,
    on_progress: callable = None,
) -> list[dict]:
    """Fetch files associated with a Commons user.

    Uses usercontribs (namespace 6) to find all files the user has
    uploaded or edited, then fetches image info for each.
    This matches Special:ListFiles and catches files that allimages misses
    (e.g. files transferred from other accounts or re-uploaded).
    """
    # Normalize: Commons uses spaces in titles but underscores in URLs.
    # We store with underscores (matching allimages output).
    def norm(name):
        return name.replace(" ", "_")

    # Step 1: Get all filenames via usercontribs + allimages
    filenames = set()

    # usercontribs catches files the user edited (including re-uploads)
    params = {
        "action": "query",
        "list": "usercontribs",
        "ucuser": username,
        "ucnamespace": "6",
        "uclimit": "500",
        "ucprop": "title|timestamp",
    }
    while len(filenames) < limit:
        data = _get(client, params)
        contribs = data.get("query", {}).get("usercontribs", [])
        if not contribs:
            break
        for c in contribs:
            filenames.add(norm(c["title"].removeprefix("File:")))
        if on_progress:
            on_progress(len(filenames))
        cont = data.get("continue")
        if not cont:
            break
        params["uccontinue"] = cont["uccontinue"]

    # allimages catches files where user is the registered uploader
    ai_params = {
        "action": "query",
        "list": "allimages",
        "aisort": "timestamp",
        "aidir": "descending",
        "aiuser": username,
        "ailimit": "500",
        "aiprop": "timestamp|url|size|mime",
    }
    while True:
        data = _get(client, ai_params)
        images = data.get("query", {}).get("allimages", [])
        if not images:
            break
        for img in images:
            filenames.add(norm(img["name"]))
        cont = data.get("continue")
        if not cont:
            break
        ai_params["aicontinue"] = cont["aicontinue"]

    # logevents catches upload events (files uploaded by user)
    le_params = {
        "action": "query",
        "list": "logevents",
        "letype": "upload",
        "leuser": username,
        "lelimit": "500",
    }
    while True:
        data = _get(client, le_params)
        events = data.get("query", {}).get("logevents", [])
        if not events:
            break
        for ev in events:
            filenames.add(norm(ev.get("title", "").removeprefix("File:")))
        cont = data.get("continue")
        if not cont:
            break
        le_params["lecontinue"] = cont["lecontinue"]

    if on_progress:
        on_progress(len(filenames))

    # Step 2: Fetch image info for all files in batches
    filenames_list = sorted(filenames)[:limit]
    uploads = []

    for i in range(0, len(filenames_list), BATCH_SIZE):
        batch = filenames_list[i:i + BATCH_SIZE]
        titles = "|".join(f"File:{f}" for f in batch)
        data = _get(client, {
            "action": "query",
            "titles": titles,
            "prop": "imageinfo",
            "iiprop": "timestamp|url|size|mime",
        })
        for page in data.get("query", {}).get("pages", []):
            if page.get("missing"):
                continue
            ii = page.get("imageinfo", [{}])[0] if page.get("imageinfo") else {}
            name = page.get("title", "").removeprefix("File:")
            uploads.append({
                "name": name,
                "timestamp": ii.get("timestamp", ""),
                "url": ii.get("url", ""),
                "size": ii.get("size", 0),
                "mime": ii.get("mime", ""),
            })
        if on_progress:
            on_progress(len(filenames_list))

    return uploads


def _fetch_batch_details(batch: list[str]) -> dict:
    """Fetch details for a single batch of filenames. Used by thread pool.

    Handles continuation for globalusage/fileusage/categories which can
    exceed the per-request limit when batching multiple files.
    """
    client = make_client()
    titles_str = "|".join(batch)

    # Accumulate results across continuation requests
    results = {}

    params = {
        "action": "query",
        "titles": titles_str,
        "prop": "globalusage|fileusage|categories|imageinfo",
        "iiprop": "extmetadata|url|size|mime",
        "gulimit": "500",
        "fulimit": "500",
        "cllimit": "500",
    }

    while True:
        data = _get(client, params)
        pages = data.get("query", {}).get("pages", [])

        for page in pages:
            title = page.get("title", "")

            if title not in results:
                results[title] = {
                    "global_usage": [],
                    "categories": [],
                    "imageinfo": {},
                    "_file_usage": [],
                }

            results[title]["global_usage"].extend(page.get("globalusage", []))
            results[title]["_file_usage"].extend(page.get("fileusage", []))
            results[title]["categories"].extend(
                c["title"] for c in page.get("categories", [])
            )
            if page.get("imageinfo") and not results[title]["imageinfo"]:
                results[title]["imageinfo"] = page["imageinfo"][0]

        cont = data.get("continue")
        if not cont:
            break
        # Merge continue tokens into params for next request
        for key, val in cont.items():
            params[key] = val

    # Post-process: merge file_usage into global_usage, deduplicate categories
    for title, r in results.items():
        file_usage = [
            {
                "title": fu.get("title", ""),
                "wiki": "commons.wikimedia.org",
                "url": f"https://commons.wikimedia.org/wiki/{fu.get('title', '').replace(' ', '_')}",
            }
            for fu in r.pop("_file_usage", [])
        ]
        r["global_usage"].extend(file_usage)
        r["categories"] = list(dict.fromkeys(r["categories"]))  # deduplicate, preserve order

    return results


def get_file_details(
    filenames: list[str],
    on_progress: callable = None,
) -> dict:
    """Fetch global usage, categories, and metadata for files. Uses parallel requests."""
    titled = [f if f.startswith("File:") else f"File:{f}" for f in filenames]

    # Split into batches
    batches = [titled[i : i + BATCH_SIZE] for i in range(0, len(titled), BATCH_SIZE)]

    results = {}
    completed = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_batch_details, batch): batch for batch in batches}
        for future in as_completed(futures):
            batch_results = future.result()
            results.update(batch_results)
            completed += 1
            if on_progress:
                on_progress(completed, len(batches))

    # Normalize keys: API returns spaces but upload list uses underscores.
    # Add underscore-keyed aliases so lookups work either way.
    normalized = {}
    for key, val in results.items():
        normalized[key] = val
        alt_key = key.replace(" ", "_")
        if alt_key != key:
            normalized[alt_key] = val
    return normalized


def get_image_description(imageinfo: dict) -> str:
    """Extract a plain-text description from imageinfo extmetadata."""
    ext = imageinfo.get("extmetadata", {})
    desc = ext.get("ImageDescription", {}).get("value", "")
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
