"""PhotoPost - Wikipedia photo tracker dashboard."""

import json
import threading
from datetime import datetime, timezone

from flask import Flask, render_template, request, jsonify
from sqlalchemy import func, case

from models import init_db, TrackedUser, Photo, PhotoUsage, UsageEvent, Checklist, ChecklistItem
from poller import poll_user, poll_progress
from suggestions import suggest_articles_for_photo

app = Flask(__name__)

engine, Session = init_db()

_active_polls = set()
_polls_lock = threading.Lock()


@app.route("/")
def index():
    return render_template("index.html")


# --- User management ---

@app.route("/api/users", methods=["GET"])
def list_users():
    db = Session()
    try:
        users = db.query(TrackedUser).order_by(TrackedUser.added_at.desc()).all()
        result = []
        for u in users:
            photo_count = db.query(Photo).filter_by(user_id=u.id).count()
            active_usages = (
                db.query(func.count(PhotoUsage.id))
                .join(Photo)
                .filter(Photo.user_id == u.id, PhotoUsage.is_active == True)
                .scalar()
            )
            result.append({
                "id": u.id,
                "username": u.username,
                "added_at": u.added_at.isoformat() if u.added_at else None,
                "last_polled": u.last_polled.isoformat() if u.last_polled else None,
                "photo_count": photo_count,
                "active_usages": active_usages or 0,
                "is_polling": u.username in _active_polls,
            })
        return jsonify(result)
    finally:
        db.close()


@app.route("/api/users", methods=["POST"])
def add_user():
    data = request.get_json()
    username = data.get("username", "").strip()
    if not username:
        return jsonify({"error": "Username is required"}), 400
    db = Session()
    try:
        existing = db.query(TrackedUser).filter_by(username=username).first()
        if existing:
            return jsonify({"error": f"User '{username}' is already tracked", "id": existing.id}), 409
        user = TrackedUser(username=username)
        db.add(user)
        db.commit()
        return jsonify({"id": user.id, "username": user.username}), 201
    finally:
        db.close()


@app.route("/api/users/<int:user_id>", methods=["DELETE"])
def delete_user(user_id):
    db = Session()
    try:
        user = db.get(TrackedUser, user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        db.delete(user)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


# --- Polling ---

@app.route("/api/users/<int:user_id>/poll", methods=["POST"])
def trigger_poll(user_id):
    db = Session()
    try:
        user = db.get(TrackedUser, user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        with _polls_lock:
            if user.username in _active_polls:
                return jsonify({"error": "Poll already in progress"}), 409
            _active_polls.add(user.username)
        username = user.username
    finally:
        db.close()

    def run_poll():
        poll_db = Session()
        try:
            poll_user_obj = poll_db.get(TrackedUser, user_id)
            stats = poll_user(poll_db, poll_user_obj)
            app.config[f"poll_result_{user_id}"] = stats
        except Exception as e:
            app.config[f"poll_result_{user_id}"] = {"error": str(e)}
        finally:
            poll_db.close()
            with _polls_lock:
                _active_polls.discard(username)

    threading.Thread(target=run_poll, daemon=True).start()
    return jsonify({"status": "polling", "message": f"Poll started for {username}"})


@app.route("/api/users/<int:user_id>/poll-status", methods=["GET"])
def poll_status(user_id):
    db = Session()
    try:
        user = db.get(TrackedUser, user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        is_polling = user.username in _active_polls
        result = app.config.pop(f"poll_result_{user_id}", None)
        progress = poll_progress.get(user_id, {})
        return jsonify({
            "is_polling": is_polling,
            "result": result,
            "progress": progress if is_polling else {},
            "last_polled": user.last_polled.isoformat() if user.last_polled else None,
        })
    finally:
        db.close()


# --- Photos ---

@app.route("/api/users/<int:user_id>/photos", methods=["GET"])
def user_photos(user_id):
    db = Session()
    try:
        user = db.get(TrackedUser, user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        page = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 50)), 200)
        sort = request.args.get("sort", "date")
        search = request.args.get("q", "").strip()
        offset = (page - 1) * per_page

        # Usage count subquery
        usage_sub = (
            db.query(PhotoUsage.photo_id, func.count(PhotoUsage.id).label("cnt"))
            .filter(PhotoUsage.is_active == True)
            .group_by(PhotoUsage.photo_id)
            .subquery()
        )

        query = (
            db.query(Photo, func.coalesce(usage_sub.c.cnt, 0).label("usage_count"))
            .outerjoin(usage_sub, Photo.id == usage_sub.c.photo_id)
            .filter(Photo.user_id == user_id)
        )

        if search:
            query = query.filter(Photo.filename.ilike(f"%{search}%"))

        # Count before pagination
        total = query.count()

        if sort == "usages":
            query = query.order_by(func.coalesce(usage_sub.c.cnt, 0).desc())
        elif sort == "usages_asc":
            query = query.order_by(func.coalesce(usage_sub.c.cnt, 0).asc())
        elif sort == "name":
            query = query.order_by(Photo.filename)
        elif sort == "name_desc":
            query = query.order_by(Photo.filename.desc())
        elif sort == "date_asc":
            query = query.order_by(Photo.upload_date.asc())
        else:
            query = query.order_by(Photo.upload_date.desc())

        rows = query.offset(offset).limit(per_page).all()

        result = []
        for p, ucount in rows:
            result.append({
                "id": p.id,
                "filename": p.filename,
                "description": p.description or "",
                "thumb_url": p.thumb_url,
                "upload_date": p.upload_date.isoformat() if p.upload_date else None,
                "usage_count": ucount,
            })
        return jsonify({
            "username": user.username,
            "photos": result,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": max(1, (total + per_page - 1) // per_page),
        })
    finally:
        db.close()


@app.route("/api/photos/<int:photo_id>", methods=["GET"])
def photo_detail(photo_id):
    db = Session()
    try:
        photo = db.get(Photo, photo_id)
        if not photo:
            return jsonify({"error": "Photo not found"}), 404

        # Paginate usages
        usage_page = int(request.args.get("usage_page", 1))
        usage_per_page = 50

        active_q = (
            db.query(PhotoUsage)
            .filter_by(photo_id=photo_id, is_active=True)
            .order_by(PhotoUsage.wiki, PhotoUsage.article_title)
        )
        active_total = active_q.count()
        active_usages = active_q.offset((usage_page - 1) * usage_per_page).limit(usage_per_page).all()

        removed_count = db.query(PhotoUsage).filter_by(photo_id=photo_id, is_active=False).count()

        # Wiki breakdown
        wiki_counts = (
            db.query(PhotoUsage.wiki, func.count(PhotoUsage.id))
            .filter_by(photo_id=photo_id, is_active=True)
            .group_by(PhotoUsage.wiki)
            .order_by(func.count(PhotoUsage.id).desc())
            .all()
        )

        events = (
            db.query(UsageEvent)
            .filter_by(photo_id=photo_id)
            .order_by(UsageEvent.timestamp.desc())
            .limit(50)
            .all()
        )

        # Get user_id for breadcrumb
        user_id = photo.user_id
        user = db.get(TrackedUser, user_id)

        return jsonify({
            "id": photo.id,
            "user_id": user_id,
            "username": user.username if user else "",
            "filename": photo.filename,
            "description": photo.description,
            "thumb_url": photo.thumb_url,
            "full_url": photo.full_url,
            "upload_date": photo.upload_date.isoformat() if photo.upload_date else None,
            "categories": json.loads(photo.categories) if photo.categories else [],
            "active_total": active_total,
            "removed_count": removed_count,
            "usage_page": usage_page,
            "usage_pages": max(1, (active_total + usage_per_page - 1) // usage_per_page),
            "wiki_breakdown": [{"wiki": w, "count": c} for w, c in wiki_counts],
            "active_usages": [
                {
                    "article_title": u.article_title,
                    "wiki": u.wiki,
                    "article_url": u.article_url,
                    "first_seen": u.first_seen.isoformat(),
                }
                for u in active_usages
            ],
            "events": [
                {
                    "event_type": e.event_type,
                    "article_title": e.article_title,
                    "wiki": e.wiki,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ],
        })
    finally:
        db.close()


# --- Missing language articles ---

@app.route("/api/photos/<int:photo_id>/missing-languages", methods=["GET"])
def missing_languages(photo_id):
    """Find articles in other languages that could use this photo.

    For each article that uses this photo, finds interlanguage links
    and checks which language versions DON'T use the photo.
    """
    import httpx

    db = Session()
    try:
        photo = db.get(Photo, photo_id)
        if not photo:
            return jsonify({"error": "Photo not found"}), 404

        # Get all wikis currently using this photo
        usages = (
            db.query(PhotoUsage)
            .filter_by(photo_id=photo_id, is_active=True)
            .all()
        )

        # Build set of (wiki, title) pairs that already have this photo
        has_photo = set()
        for u in usages:
            has_photo.add((u.wiki, u.article_title))

        # Group usages by wiki — find the "main" articles (en.wikipedia preferred)
        wiki_articles = {}
        for u in usages:
            wiki = u.wiki
            if wiki.endswith('.wikipedia.org'):
                wiki_articles.setdefault(wiki, []).append(u.article_title)

        # For up to 5 articles, get their interlanguage links
        client = httpx.Client(
            headers={"User-Agent": "PhotoPost/1.0"},
            timeout=15.0,
        )

        missing = []
        checked_articles = []

        # Prefer en.wikipedia articles
        source_articles = wiki_articles.get('en.wikipedia.org', [])[:5]
        if not source_articles:
            # Fall back to any wikipedia
            for wiki, articles in wiki_articles.items():
                source_articles.extend(articles[:3])
                if len(source_articles) >= 5:
                    break

        for article_title in source_articles[:5]:
            try:
                resp = client.get('https://en.wikipedia.org/w/api.php', params={
                    'action': 'query',
                    'titles': article_title,
                    'prop': 'langlinks',
                    'lllimit': '500',
                    'format': 'json',
                    'formatversion': '2',
                })
                data = resp.json()
                pages = data.get('query', {}).get('pages', [])
                if not pages:
                    continue

                langlinks = pages[0].get('langlinks', [])
                for ll in langlinks:
                    lang = ll.get('lang', '')
                    foreign_title = ll.get('title', '')
                    foreign_wiki = f"{lang}.wikipedia.org"

                    if (foreign_wiki, foreign_title) not in has_photo:
                        edit_url = f"https://{foreign_wiki}/wiki/{foreign_title.replace(' ', '_')}"
                        missing.append({
                            'lang': lang,
                            'wiki': foreign_wiki,
                            'article_title': foreign_title,
                            'source_article': article_title,
                            'edit_url': edit_url,
                        })

                checked_articles.append(article_title)
            except Exception:
                continue

        # Deduplicate by (wiki, article_title)
        seen = set()
        deduped = []
        for m in missing:
            key = (m['wiki'], m['article_title'])
            if key not in seen:
                seen.add(key)
                deduped.append(m)

        # Sort by number of speakers (approximate, top ~60 languages)
        lang_rank = {
            'en': 1, 'zh': 2, 'hi': 3, 'es': 4, 'fr': 5, 'ar': 6,
            'bn': 7, 'pt': 8, 'ru': 9, 'ja': 10, 'pa': 11, 'de': 12,
            'jv': 13, 'ko': 14, 'vi': 15, 'te': 16, 'mr': 17, 'ta': 18,
            'tr': 19, 'ur': 20, 'it': 21, 'th': 22, 'gu': 23, 'pl': 24,
            'ml': 25, 'kn': 26, 'uk': 27, 'my': 28, 'fa': 29, 'ha': 30,
            'nl': 31, 'or': 32, 'ro': 33, 'ms': 34, 'hu': 35, 'az': 36,
            'el': 37, 'cs': 38, 'sv': 39, 'be': 40, 'da': 41, 'fi': 42,
            'no': 43, 'nb': 43, 'nn': 43, 'sk': 44, 'bg': 45, 'sr': 46,
            'he': 47, 'ca': 48, 'hr': 49, 'lt': 50, 'sl': 51, 'lv': 52,
            'et': 53, 'id': 34, 'tl': 55, 'sw': 56, 'ne': 57, 'si': 58,
            'af': 59, 'ga': 60, 'cy': 61, 'eu': 62, 'gl': 63,
            'simple': 1,  # Simple English — same audience as en
        }
        deduped.sort(key=lambda x: lang_rank.get(x['lang'], 999))

        return jsonify({
            'missing': deduped,
            'checked_articles': checked_articles,
            'total_missing': len(deduped),
        })
    finally:
        db.close()


# --- Checklists (global — checks all tracked users' photos) ---

@app.route("/api/checklists", methods=["GET"])
def list_checklists():
    db = Session()
    try:
        checklists = db.query(Checklist).order_by(Checklist.created_at.desc()).all()
        return jsonify([{
            "id": c.id,
            "name": c.name,
            "created_at": c.created_at.isoformat(),
            "last_checked": c.last_checked.isoformat() if c.last_checked else None,
            "item_count": len(c.items),
            "found": sum(1 for i in c.items if i.status == "found"),
            "missing": sum(1 for i in c.items if i.status == "missing"),
        } for c in checklists])
    finally:
        db.close()


@app.route("/api/checklists", methods=["POST"])
def create_checklist():
    """Create a checklist from uploaded article list.

    Accepts JSON: { name, articles: ["title", "wiki:title", "https://en.wikipedia.org/wiki/...", ...] }
    Supports plain titles, wiki:title format, and full Wikipedia URLs.
    """
    import re
    from urllib.parse import unquote

    db = Session()
    try:
        data = request.get_json()
        name = data.get("name", "Checklist").strip()
        raw_articles = data.get("articles", [])

        if not raw_articles:
            return jsonify({"error": "No articles provided"}), 400

        checklist = Checklist(name=name)
        db.add(checklist)
        db.flush()

        seen = set()  # deduplicate

        for entry in raw_articles:
            wiki = "en.wikipedia.org"
            title = ""

            if isinstance(entry, str):
                entry = entry.strip()
                if not entry:
                    continue

                # Match Wikipedia URLs:
                # https://en.wikipedia.org/wiki/Article_Name
                # https://fr.wikipedia.org/wiki/Nom_de_l'article
                url_match = re.match(
                    r'https?://([a-z\-]+\.wikipedia\.org)/wiki/(.+?)(?:\?.*)?(?:#.*)?$',
                    entry,
                )
                if url_match:
                    wiki = url_match.group(1)
                    title = unquote(url_match.group(2))
                elif ":" in entry and ".wikipedia.org" in entry.split(":")[0]:
                    wiki, title = entry.split(":", 1)
                    wiki = wiki.strip()
                    title = title.strip()
                else:
                    title = entry
            else:
                title = entry.get("title", "").strip()
                wiki = entry.get("wiki", "en.wikipedia.org").strip()

            if not title:
                continue

            title = title.replace("_", " ")

            # Deduplicate
            key = (wiki, title)
            if key in seen:
                continue
            seen.add(key)

            item = ChecklistItem(
                checklist_id=checklist.id,
                article_title=title,
                wiki=wiki,
            )
            db.add(item)

        db.commit()
        return jsonify({"id": checklist.id, "name": checklist.name, "items": len(checklist.items)}), 201
    finally:
        db.close()


@app.route("/api/checklists/<int:checklist_id>", methods=["GET"])
def get_checklist(checklist_id):
    db = Session()
    try:
        cl = db.get(Checklist, checklist_id)
        if not cl:
            return jsonify({"error": "Checklist not found"}), 404
        return jsonify({
            "id": cl.id,
            "user_id": cl.user_id,
            "name": cl.name,
            "created_at": cl.created_at.isoformat(),
            "last_checked": cl.last_checked.isoformat() if cl.last_checked else None,
            "items": [{
                "id": i.id,
                "article_title": i.article_title,
                "wiki": i.wiki,
                "expected_file": i.expected_file,
                "status": i.status,
                "last_checked": i.last_checked.isoformat() if i.last_checked else None,
                "found_files": json.loads(i.found_files) if i.found_files else [],
            } for i in cl.items],
        })
    finally:
        db.close()


@app.route("/api/checklists/<int:checklist_id>", methods=["DELETE"])
def delete_checklist(checklist_id):
    db = Session()
    try:
        cl = db.get(Checklist, checklist_id)
        if not cl:
            return jsonify({"error": "Not found"}), 404
        db.delete(cl)
        db.commit()
        return jsonify({"ok": True})
    finally:
        db.close()


_checklist_progress = {}  # { checklist_id: { checked, total, found, missing, current, done } }

@app.route("/api/checklists/<int:checklist_id>/check", methods=["POST"])
def run_checklist(checklist_id):
    """Start a background check of all articles in the checklist."""
    db = Session()
    try:
        cl = db.get(Checklist, checklist_id)
        if not cl:
            return jsonify({"error": "Not found"}), 404

        if checklist_id in _checklist_progress and not _checklist_progress[checklist_id].get("done"):
            return jsonify({"error": "Check already running"}), 409

        _checklist_progress[checklist_id] = {
            "checked": 0, "total": len(cl.items),
            "found": 0, "missing": 0, "errors": 0,
            "current": "", "done": False,
        }
    finally:
        db.close()

    def do_check():
        import httpx
        check_db = Session()
        try:
            cl = check_db.get(Checklist, checklist_id)
            # Load ALL tracked users' photos with multiple lookup keys
            user_files = {}  # normalized_name -> (filename, username)
            for p in check_db.query(Photo).join(TrackedUser).all():
                for variant in (p.filename, p.filename.replace("_", " ")):
                    user_files[variant] = (p.filename, p.user.username)
                    # Also store lowercase for case-insensitive fallback
                    user_files[variant.lower()] = (p.filename, p.user.username)

            import re as _re
            import time as _time

            client = httpx.Client(
                headers={"User-Agent": "PhotoPost/1.0 (https://github.com/fossick-cyber/photopost)"},
                timeout=20.0,
            )
            now = datetime.now(timezone.utc)
            prog = _checklist_progress[checklist_id]

            def _api_get(url, params, retries=5):
                """GET with aggressive backoff on rate limiting."""
                for attempt in range(retries):
                    try:
                        resp = client.get(url, params=params)
                        if resp.status_code == 200 and resp.text.strip():
                            return resp.json()
                        if resp.status_code == 429:
                            # Rate limited — back off aggressively
                            wait = int(resp.headers.get("Retry-After", 5 * (attempt + 1)))
                            prog["current"] = f"Rate limited, waiting {wait}s..."
                            _time.sleep(wait)
                        else:
                            _time.sleep(2 * (attempt + 1))
                    except Exception:
                        if attempt < retries - 1:
                            _time.sleep(2 * (attempt + 1))
                        else:
                            raise
                return {}

            def _strip_file_ns(name):
                """Strip any namespace prefix (File:, Plik:, Delwedd:, etc).
                The images API always returns <Namespace>:<Filename>, so just
                strip everything up to the first colon."""
                if ":" in name:
                    return name.split(":", 1)[1]
                return name

            def _match_images(article_images):
                matched = []
                seen = set()
                for img_name in article_images:
                    m = user_files.get(img_name) or user_files.get(img_name.lower())
                    if m and img_name.lower() not in seen:
                        matched.append({"file": m[0], "user": m[1]})
                        seen.add(img_name.lower())
                return matched

            # Group items by wiki for batching
            from collections import defaultdict
            wiki_groups = defaultdict(list)
            for item in cl.items:
                wiki_groups[item.wiki].append(item)

            for wiki, items_in_wiki in wiki_groups.items():
                api_url = f"https://{wiki}/w/api.php"

                # Process in batches of 20 (conservative to avoid hitting image limits)
                for batch_start in range(0, len(items_in_wiki), 20):
                    batch = items_in_wiki[batch_start:batch_start + 20]
                    titles = "|".join(item.article_title for item in batch)
                    prog["current"] = f"{wiki} ({batch_start+1}-{min(batch_start+20, len(items_in_wiki))} of {len(items_in_wiki)})"

                    try:
                        # Batch: fetch images for up to 20 articles at once
                        data = _api_get(api_url, {
                            "action": "query",
                            "titles": titles,
                            "prop": "images",
                            "imlimit": "500",
                            "format": "json",
                            "formatversion": "2",
                            "maxlag": "5",
                        })
                        pages = {p.get("title", ""): p for p in data.get("query", {}).get("pages", [])}

                        for item in batch:
                            # Match page by title (handle normalization)
                            page = pages.get(item.article_title) or pages.get(item.article_title.replace(" ", "_"))
                            # Also try normalized titles from the API
                            if not page:
                                for norm in data.get("query", {}).get("normalized", []):
                                    if norm.get("from") == item.article_title or norm.get("from") == item.article_title.replace(" ", "_"):
                                        page = pages.get(norm.get("to"))
                                        break

                            article_images = set()
                            if page and not page.get("missing"):
                                for img in page.get("images", []):
                                    name = _strip_file_ns(img.get("title", ""))
                                    article_images.add(name)
                                    article_images.add(name.replace(" ", "_"))

                            matched = _match_images(article_images)
                            item.status = "found" if matched else "missing"
                            item.found_files = json.dumps(matched)
                            item.last_checked = now
                            if matched:
                                prog["found"] += 1
                            else:
                                prog["missing"] += 1
                            prog["checked"] += 1

                    except Exception as e:
                        import traceback
                        traceback.print_exc()
                        for item in batch:
                            if not item.last_checked or item.last_checked < now:
                                item.status = "error"
                                item.last_checked = now
                                prog["errors"] += 1
                                prog["checked"] += 1

                    check_db.commit()
                    _time.sleep(1)  # 1s between batches

                # Wikitext fallback: for items still "missing", try wikitext scan
                missing_items = [i for i in items_in_wiki if i.status == "missing"]
                for item in missing_items:
                    prog["current"] = f"Wikitext check: {item.article_title}"
                    try:
                        data2 = _api_get(api_url, {
                            "action": "query",
                            "titles": item.article_title,
                            "prop": "revisions",
                            "rvprop": "content",
                            "rvslots": "main",
                            "rvlimit": "1",
                            "format": "json",
                            "formatversion": "2",
                            "maxlag": "5",
                        })
                        pages2 = data2.get("query", {}).get("pages", [])
                        if pages2 and not pages2[0].get("missing"):
                            content = pages2[0].get("revisions", [{}])[0].get("slots", {}).get("main", {}).get("content", "")
                            article_images = set()
                            # Match [[AnyNamespace:filename.ext|...]] — catches File, Plik, Delwedd, etc.
                            for m in _re.findall(r'\[\[[A-Za-z\u00C0-\u024F\u0400-\u04FF\u3000-\u9FFF]+:([^|\]]+\.(?:jpg|jpeg|png|svg|gif|tif|tiff|webp))', content, _re.IGNORECASE):
                                article_images.add(m.strip())
                                article_images.add(m.strip().replace(" ", "_"))
                            for m in _re.findall(r'\|[^=]*(?:image|photo|logo|cover|map_image|picture)\s*=\s*([^\n|}{]+\.(?:jpg|jpeg|png|svg|gif))', content, _re.IGNORECASE):
                                article_images.add(m.strip())
                                article_images.add(m.strip().replace(" ", "_"))
                            matched = _match_images(article_images)
                            if matched:
                                item.status = "found"
                                item.found_files = json.dumps(matched)
                                prog["found"] += 1
                                prog["missing"] -= 1
                        check_db.commit()
                    except Exception:
                        pass
                    _time.sleep(0.5)

            cl.last_checked = now
            check_db.commit()
            prog["done"] = True
            prog["current"] = ""
        except Exception as e:
            _checklist_progress[checklist_id]["done"] = True
            _checklist_progress[checklist_id]["error"] = str(e)
        finally:
            check_db.close()

    threading.Thread(target=do_check, daemon=True).start()
    return jsonify({"status": "started", "total": _checklist_progress[checklist_id]["total"]})


@app.route("/api/checklists/<int:checklist_id>/check-status", methods=["GET"])
def checklist_check_status(checklist_id):
    prog = _checklist_progress.get(checklist_id)
    if not prog:
        return jsonify({"running": False})
    return jsonify({
        "running": not prog["done"],
        "checked": prog["checked"],
        "total": prog["total"],
        "found": prog["found"],
        "missing": prog["missing"],
        "errors": prog.get("errors", 0),
        "current": prog.get("current", ""),
        "pct": int(prog["checked"] / max(prog["total"], 1) * 100),
    })


# --- Article history check for removed photos ---

@app.route("/api/check-removal", methods=["POST"])
def check_removal():
    """Check article revision history to find when/if a photo was removed.

    Accepts JSON: { wiki, article_title }
    Searches recent revisions for any tracked user photos that were removed.
    """
    import re
    import httpx

    data = request.get_json()
    wiki = data.get("wiki", "en.wikipedia.org")
    article_title = data.get("article_title", "").strip()
    if not article_title:
        return jsonify({"error": "article_title required"}), 400

    api_url = f"https://{wiki}/w/api.php"
    client = httpx.Client(headers={"User-Agent": "PhotoPost/1.0"}, timeout=15)

    db = Session()
    try:
        # Get all tracked filenames (both space and underscore forms)
        user_files = {}
        for p in db.query(Photo).join(TrackedUser).all():
            user_files[p.filename.lower()] = (p.filename, p.user.username)
            user_files[p.filename.replace("_", " ").lower()] = (p.filename, p.user.username)
    finally:
        db.close()

    def find_images_in_text(text):
        """Find all image references in wikitext."""
        found = set()
        # [[File:Name.jpg|...]] and [[Image:Name.jpg|...]]
        # Match any namespace prefix (File, Plik, Delwedd, etc.) followed by image filename
        for m in re.findall(r'\[\[[A-Za-z\u00C0-\u024F\u0400-\u04FF\u3000-\u9FFF]+:([^|\]]+\.(?:jpg|jpeg|png|svg|gif|tif|tiff|webp))', text, re.IGNORECASE):
            found.add(m.strip())
        # |image= or |photo= or |logo= in templates
        for m in re.findall(r'\|[^=]*(?:image|photo|logo|cover)\s*=\s*([^\n|}{]+\.(?:jpg|jpeg|png|svg|gif))', text, re.IGNORECASE):
            found.add(m.strip())
        return found

    try:
        # Get last 50 revisions with content
        resp = client.get(api_url, params={
            "action": "query",
            "titles": article_title,
            "prop": "revisions",
            "rvprop": "ids|timestamp|user|comment|content",
            "rvslots": "main",
            "rvlimit": "50",
            "format": "json",
            "formatversion": "2",
        })
        data = resp.json()
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing"):
            return jsonify({"error": "Article not found", "removals": []})

        revisions = pages[0].get("revisions", [])
        if not revisions:
            return jsonify({"removals": [], "revisions_checked": 0})

        # Walk through revisions (newest first) looking for image removals
        removals = []
        prev_images = None

        for rev in revisions:
            content = rev.get("slots", {}).get("main", {}).get("content", "")
            current_images = find_images_in_text(content)

            if prev_images is not None:
                # Images in this (older) revision but not in the next (newer) one = removed
                removed = current_images - prev_images
                for img_name in removed:
                    # Check if it's one of our tracked photos
                    lookup = img_name.lower()
                    if lookup in user_files:
                        fname, uname = user_files[lookup]
                        removals.append({
                            "file": fname,
                            "user": uname,
                            "removed_in_rev": revisions[revisions.index(rev) - 1]["revid"],
                            "removed_by": revisions[revisions.index(rev) - 1].get("user", ""),
                            "removed_at": revisions[revisions.index(rev) - 1].get("timestamp", ""),
                            "edit_comment": revisions[revisions.index(rev) - 1].get("comment", ""),
                            "diff_url": f"https://{wiki}/w/index.php?diff={revisions[revisions.index(rev) - 1]['revid']}&oldid={rev['revid']}",
                        })

            prev_images = current_images

        return jsonify({
            "removals": removals,
            "revisions_checked": len(revisions),
            "article": article_title,
            "wiki": wiki,
        })

    except Exception as e:
        return jsonify({"error": str(e), "removals": []}), 502


# --- Events feed ---

@app.route("/api/users/<int:user_id>/events", methods=["GET"])
def user_events(user_id):
    db = Session()
    try:
        user = db.get(TrackedUser, user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))
        events = (
            db.query(UsageEvent)
            .join(Photo)
            .filter(Photo.user_id == user_id)
            .order_by(UsageEvent.timestamp.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return jsonify({
            "events": [
                {
                    "id": e.id,
                    "photo_id": e.photo_id,
                    "photo_filename": e.photo.filename,
                    "event_type": e.event_type,
                    "article_title": e.article_title,
                    "wiki": e.wiki,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in events
            ]
        })
    finally:
        db.close()


# --- Stats ---

@app.route("/api/users/<int:user_id>/stats", methods=["GET"])
def user_stats(user_id):
    db = Session()
    try:
        user = db.get(TrackedUser, user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        total_photos = db.query(Photo).filter_by(user_id=user_id).count()
        active_usages = (
            db.query(func.count(PhotoUsage.id))
            .join(Photo)
            .filter(Photo.user_id == user_id, PhotoUsage.is_active == True)
            .scalar() or 0
        )
        unused_photos = (
            db.query(Photo)
            .filter_by(user_id=user_id)
            .outerjoin(PhotoUsage, (PhotoUsage.photo_id == Photo.id) & (PhotoUsage.is_active == True))
            .filter(PhotoUsage.id == None)
            .count()
        )
        unique_wikis = (
            db.query(func.count(func.distinct(PhotoUsage.wiki)))
            .join(Photo)
            .filter(Photo.user_id == user_id, PhotoUsage.is_active == True)
            .scalar() or 0
        )

        return jsonify({
            "total_photos": total_photos,
            "active_usages": active_usages,
            "unused_photos": unused_photos,
            "unique_wikis": unique_wikis,
            "avg_usages": round(active_usages / max(total_photos, 1), 1),
        })
    finally:
        db.close()


# --- Suggestions ---

@app.route("/api/suggest", methods=["POST"])
def suggest():
    data = request.get_json()
    categories = data.get("categories", [])
    description = data.get("description", "")
    current_articles = data.get("current_articles", [])
    filename = data.get("filename", "")
    current_usages = data.get("current_usages", [])
    if not categories and not description:
        return jsonify({"suggestions": []})
    try:
        results = suggest_articles_for_photo(
            categories, description, current_articles,
            filename=filename,
            current_usages=current_usages,
        )
    except Exception as e:
        return jsonify({"error": f"Suggestion failed: {e}"}), 502
    return jsonify({"suggestions": results})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5053)
