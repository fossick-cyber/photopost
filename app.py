"""PhotoPost - Wikipedia photo tracker dashboard."""

import json
import threading

from flask import Flask, render_template, request, jsonify

from models import init_db, TrackedUser, Photo, PhotoUsage, UsageEvent
from poller import poll_user
from suggestions import suggest_articles_for_photo

app = Flask(__name__)

engine, Session = init_db()

# Track active polls to prevent double-polling
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
                db.query(PhotoUsage)
                .join(Photo)
                .filter(Photo.user_id == u.id, PhotoUsage.is_active == True)
                .count()
            )
            result.append({
                "id": u.id,
                "username": u.username,
                "added_at": u.added_at.isoformat() if u.added_at else None,
                "last_polled": u.last_polled.isoformat() if u.last_polled else None,
                "photo_count": photo_count,
                "active_usages": active_usages,
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
        user = db.query(TrackedUser).get(user_id)
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
        user = db.query(TrackedUser).get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        with _polls_lock:
            if user.username in _active_polls:
                return jsonify({"error": "Poll already in progress for this user"}), 409
            _active_polls.add(user.username)

        username = user.username
    finally:
        db.close()

    def run_poll():
        poll_db = Session()
        try:
            poll_user_obj = poll_db.query(TrackedUser).get(user_id)
            stats = poll_user(poll_db, poll_user_obj)
            # Store stats so the frontend can retrieve them
            app.config[f"poll_result_{user_id}"] = stats
        except Exception as e:
            app.config[f"poll_result_{user_id}"] = {"error": str(e)}
        finally:
            poll_db.close()
            with _polls_lock:
                _active_polls.discard(username)

    thread = threading.Thread(target=run_poll, daemon=True)
    thread.start()
    return jsonify({"status": "polling", "message": f"Poll started for {username}"})


@app.route("/api/users/<int:user_id>/poll-status", methods=["GET"])
def poll_status(user_id):
    db = Session()
    try:
        user = db.query(TrackedUser).get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        is_polling = user.username in _active_polls
        result = app.config.pop(f"poll_result_{user_id}", None)
        return jsonify({
            "is_polling": is_polling,
            "result": result,
            "last_polled": user.last_polled.isoformat() if user.last_polled else None,
        })
    finally:
        db.close()


# --- Photos ---

@app.route("/api/users/<int:user_id>/photos", methods=["GET"])
def user_photos(user_id):
    db = Session()
    try:
        user = db.query(TrackedUser).get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        photos = (
            db.query(Photo)
            .filter_by(user_id=user_id)
            .order_by(Photo.upload_date.desc())
            .all()
        )

        result = []
        for p in photos:
            active_usages = [u for u in p.usages if u.is_active]
            result.append({
                "id": p.id,
                "filename": p.filename,
                "description": p.description,
                "thumb_url": p.thumb_url,
                "full_url": p.full_url,
                "upload_date": p.upload_date.isoformat() if p.upload_date else None,
                "categories": json.loads(p.categories) if p.categories else [],
                "usage_count": len(active_usages),
                "usages": [
                    {
                        "article_title": u.article_title,
                        "wiki": u.wiki,
                        "article_url": u.article_url,
                        "first_seen": u.first_seen.isoformat() if u.first_seen else None,
                    }
                    for u in active_usages
                ],
            })
        return jsonify({"username": user.username, "photos": result, "total": len(result)})
    finally:
        db.close()


@app.route("/api/photos/<int:photo_id>", methods=["GET"])
def photo_detail(photo_id):
    db = Session()
    try:
        photo = db.query(Photo).get(photo_id)
        if not photo:
            return jsonify({"error": "Photo not found"}), 404

        active_usages = [u for u in photo.usages if u.is_active]
        removed_usages = [u for u in photo.usages if not u.is_active]

        events = (
            db.query(UsageEvent)
            .filter_by(photo_id=photo_id)
            .order_by(UsageEvent.timestamp.desc())
            .limit(100)
            .all()
        )

        return jsonify({
            "id": photo.id,
            "filename": photo.filename,
            "description": photo.description,
            "thumb_url": photo.thumb_url,
            "full_url": photo.full_url,
            "upload_date": photo.upload_date.isoformat() if photo.upload_date else None,
            "categories": json.loads(photo.categories) if photo.categories else [],
            "active_usages": [
                {
                    "article_title": u.article_title,
                    "wiki": u.wiki,
                    "article_url": u.article_url,
                    "first_seen": u.first_seen.isoformat(),
                    "last_seen": u.last_seen.isoformat(),
                }
                for u in active_usages
            ],
            "removed_usages": [
                {
                    "article_title": u.article_title,
                    "wiki": u.wiki,
                    "first_seen": u.first_seen.isoformat(),
                    "last_seen": u.last_seen.isoformat(),
                }
                for u in removed_usages
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


# --- Events feed ---

@app.route("/api/users/<int:user_id>/events", methods=["GET"])
def user_events(user_id):
    db = Session()
    try:
        user = db.query(TrackedUser).get(user_id)
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
        user = db.query(TrackedUser).get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        total_photos = db.query(Photo).filter_by(user_id=user_id).count()
        active_usages = (
            db.query(PhotoUsage)
            .join(Photo)
            .filter(Photo.user_id == user_id, PhotoUsage.is_active == True)
            .count()
        )
        unused_photos = (
            db.query(Photo)
            .filter_by(user_id=user_id)
            .outerjoin(PhotoUsage, (PhotoUsage.photo_id == Photo.id) & (PhotoUsage.is_active == True))
            .filter(PhotoUsage.id == None)
            .count()
        )

        # Unique wikis
        wikis = (
            db.query(PhotoUsage.wiki)
            .join(Photo)
            .filter(Photo.user_id == user_id, PhotoUsage.is_active == True)
            .distinct()
            .all()
        )

        # Recent events
        recent_adds = (
            db.query(UsageEvent)
            .join(Photo)
            .filter(Photo.user_id == user_id, UsageEvent.event_type == "added")
            .count()
        )
        recent_removes = (
            db.query(UsageEvent)
            .join(Photo)
            .filter(Photo.user_id == user_id, UsageEvent.event_type == "removed")
            .count()
        )

        return jsonify({
            "total_photos": total_photos,
            "active_usages": active_usages,
            "unused_photos": unused_photos,
            "unique_wikis": len(wikis),
            "wiki_list": [w[0] for w in wikis],
            "total_adds": recent_adds,
            "total_removes": recent_removes,
            "avg_usages": round(active_usages / max(total_photos, 1), 1),
        })
    finally:
        db.close()


# --- Suggestions (kept from v1) ---

@app.route("/api/suggest", methods=["POST"])
def suggest():
    data = request.get_json()
    categories = data.get("categories", [])
    description = data.get("description", "")
    current_articles = set(data.get("current_articles", []))

    if not categories and not description:
        return jsonify({"suggestions": []})

    from commons_api import make_client
    client = make_client()
    try:
        results = suggest_articles_for_photo(client, categories, description, current_articles)
    except Exception as e:
        return jsonify({"error": f"Suggestion failed: {e}"}), 502

    return jsonify({"suggestions": results})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5053)
