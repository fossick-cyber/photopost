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

    Accepts JSON: { name, articles: ["title", "wiki:title", ...] }
    """
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

        for entry in raw_articles:
            if isinstance(entry, str):
                if ":" in entry and ".wikipedia.org" in entry.split(":")[0]:
                    wiki, title = entry.split(":", 1)
                    wiki = wiki.strip()
                    title = title.strip()
                else:
                    wiki = "en.wikipedia.org"
                    title = entry.strip()
            else:
                title = entry.get("title", "").strip()
                wiki = entry.get("wiki", "en.wikipedia.org").strip()

            if not title:
                continue

            title = title.replace("_", " ")

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
            # Load ALL tracked users' photos
            user_files = {}  # normalized_name -> (filename, username)
            for p in check_db.query(Photo).join(TrackedUser).all():
                user_files[p.filename] = (p.filename, p.user.username)
                user_files[p.filename.replace("_", " ")] = (p.filename, p.user.username)

            client = httpx.Client(headers={"User-Agent": "PhotoPost/1.0"}, timeout=15.0)
            now = datetime.now(timezone.utc)
            prog = _checklist_progress[checklist_id]

            for item in cl.items:
                prog["current"] = item.article_title
                api_url = f"https://{item.wiki}/w/api.php"

                try:
                    resp = client.get(api_url, params={
                        "action": "query",
                        "titles": item.article_title,
                        "prop": "images",
                        "imlimit": "500",
                        "format": "json",
                        "formatversion": "2",
                    })
                    data = resp.json()
                    pages = data.get("query", {}).get("pages", [])

                    article_images = set()
                    if pages and not pages[0].get("missing"):
                        for img in pages[0].get("images", []):
                            name = img.get("title", "").removeprefix("File:")
                            article_images.add(name)
                            article_images.add(name.replace(" ", "_"))

                    # Find which tracked photos are on this article
                    matched = []
                    seen = set()
                    for img_name in article_images:
                        if img_name in user_files and img_name not in seen:
                            fname, uname = user_files[img_name]
                            matched.append({"file": fname, "user": uname})
                            seen.add(img_name)
                            seen.add(img_name.replace(" ", "_"))
                            seen.add(img_name.replace("_", " "))

                    item.status = "found" if matched else "missing"
                    item.found_files = json.dumps(matched)
                    item.last_checked = now

                    if matched:
                        prog["found"] += 1
                    else:
                        prog["missing"] += 1
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    item.status = "error"
                    item.last_checked = now
                    prog["errors"] += 1

                prog["checked"] += 1
                # Commit each item so frontend sees live results
                check_db.commit()

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
    if not categories and not description:
        return jsonify({"suggestions": []})
    try:
        results = suggest_articles_for_photo(categories, description, current_articles)
    except Exception as e:
        return jsonify({"error": f"Suggestion failed: {e}"}), 502
    return jsonify({"suggestions": results})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5053)
