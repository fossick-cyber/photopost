"""Poll engine: fetches Commons data, diffs against DB, logs change events."""

import json
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from commons_api import (
    get_file_details,
    get_image_categories_clean,
    get_image_description,
    get_user_uploads,
    make_client,
)
from models import Photo, PhotoUsage, TrackedUser, UsageEvent

# Shared progress state: { user_id: { stage, detail, pct } }
poll_progress = {}


def poll_user(db: Session, user: TrackedUser, limit: int = 10000) -> dict:
    """Run a full poll for a tracked user with progress reporting."""
    client = make_client()
    now = datetime.now(timezone.utc)
    uid = user.id

    def update(stage, detail="", pct=0):
        poll_progress[uid] = {"stage": stage, "detail": detail, "pct": int(pct)}

    # 1. Fetch uploads
    update("uploads", "Starting upload fetch...", 0)

    def on_upload_progress(count):
        update("uploads", f"Fetched {count} uploads...", 10)

    uploads = get_user_uploads(
        client, user.username, limit=limit, on_progress=on_upload_progress,
    )
    update("uploads", f"Found {len(uploads)} uploads", 15)

    if not uploads:
        user.last_polled = now
        db.commit()
        poll_progress.pop(uid, None)
        return {"new_photos": 0, "new_usages": 0, "removed_usages": 0,
                "total_photos": 0, "total_usages": 0}

    # 2. Fetch file details (parallel)
    filenames = [img["name"] for img in uploads]

    def on_details_progress(done, total):
        pct = 15 + int((done / max(total, 1)) * 60)
        update("details", f"Fetching details: batch {done}/{total}", pct)

    update("details", f"Fetching details for {len(filenames)} files...", 15)
    details = get_file_details(filenames, on_progress=on_details_progress)

    # 3. Process & diff
    update("processing", "Comparing with database...", 80)
    existing_photos = {p.filename: p for p in user.photos}

    stats = {
        "new_photos": 0,
        "new_usages": 0,
        "removed_usages": 0,
        "total_photos": len(uploads),
        "total_usages": 0,
    }

    for i, img in enumerate(uploads):
        if i % 100 == 0:
            pct = 80 + int((i / len(uploads)) * 18)
            update("processing", f"Processing photo {i+1}/{len(uploads)}", pct)

        filename = img["name"]
        file_key = f"File:{filename}"
        detail = details.get(file_key, {})

        categories = get_image_categories_clean(detail.get("categories", []))
        imageinfo = detail.get("imageinfo", {})
        description = get_image_description(imageinfo)

        full_url = img.get("url", "")
        thumb_url = full_url
        if thumb_url:
            thumb_url = thumb_url.replace("/commons/", "/commons/thumb/", 1)
            thumb_url += "/300px-" + filename

        upload_date = None
        ts = img.get("timestamp")
        if ts:
            try:
                upload_date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass

        photo = existing_photos.get(filename)
        if photo is None:
            photo = Photo(
                user_id=user.id,
                filename=filename,
                description=description,
                thumb_url=thumb_url,
                full_url=full_url,
                upload_date=upload_date,
                size_bytes=img.get("size", 0),
                mime_type=img.get("mime", ""),
                categories=json.dumps(categories),
                first_seen=now,
            )
            db.add(photo)
            db.flush()
            stats["new_photos"] += 1
        else:
            photo.description = description
            photo.thumb_url = thumb_url
            photo.categories = json.dumps(categories)

        # Diff usages
        current_api_usages = set()
        for u in detail.get("global_usage", []):
            key = (u.get("title", ""), u.get("wiki", ""))
            current_api_usages.add(key)

        existing_usages = {
            (u.article_title, u.wiki): u
            for u in photo.usages
            if u.is_active
        }

        for title, wiki in current_api_usages:
            stats["total_usages"] += 1
            if (title, wiki) in existing_usages:
                existing_usages[(title, wiki)].last_seen = now
            else:
                old_usage = None
                for u in photo.usages:
                    if u.article_title == title and u.wiki == wiki and not u.is_active:
                        old_usage = u
                        break

                if old_usage:
                    old_usage.is_active = True
                    old_usage.last_seen = now
                else:
                    url = f"https://{wiki}/wiki/{title.replace(' ', '_')}" if wiki else ""
                    usage = PhotoUsage(
                        photo_id=photo.id,
                        article_title=title,
                        wiki=wiki,
                        article_url=url,
                        first_seen=now,
                        last_seen=now,
                    )
                    db.add(usage)

                db.add(UsageEvent(
                    photo_id=photo.id,
                    article_title=title,
                    wiki=wiki,
                    event_type="added",
                    timestamp=now,
                ))
                stats["new_usages"] += 1

        for (title, wiki), usage in existing_usages.items():
            if (title, wiki) not in current_api_usages:
                usage.is_active = False
                db.add(UsageEvent(
                    photo_id=photo.id,
                    article_title=title,
                    wiki=wiki,
                    event_type="removed",
                    timestamp=now,
                ))
                stats["removed_usages"] += 1

    update("saving", "Saving to database...", 98)
    user.last_polled = now
    db.commit()

    update("done", "Complete", 100)
    return stats
