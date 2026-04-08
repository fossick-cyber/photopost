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


def poll_user(db: Session, user: TrackedUser, limit: int = 500) -> dict:
    """Run a full poll for a tracked user.

    Fetches uploads from Commons, diffs usage against DB, and logs events.

    Returns summary dict with counts of what changed.
    """
    client = make_client()
    now = datetime.now(timezone.utc)

    # 1. Fetch all uploads
    uploads = get_user_uploads(client, user.username, limit=limit)

    # 2. Get details for all files (batched)
    filenames = [img["name"] for img in uploads]
    details = get_file_details(client, filenames)

    # 3. Build lookup of existing photos in DB
    existing_photos = {p.filename: p for p in user.photos}

    stats = {
        "new_photos": 0,
        "new_usages": 0,
        "removed_usages": 0,
        "total_photos": len(uploads),
        "total_usages": 0,
    }

    for img in uploads:
        filename = img["name"]
        file_key = f"File:{filename}"
        detail = details.get(file_key, {})

        categories = get_image_categories_clean(detail.get("categories", []))
        imageinfo = detail.get("imageinfo", {})
        description = get_image_description(imageinfo)

        # Build thumb URL
        full_url = img.get("url", "")
        thumb_url = full_url
        if thumb_url:
            thumb_url = thumb_url.replace("/commons/", "/commons/thumb/", 1)
            thumb_url += "/300px-" + filename

        # Parse upload timestamp
        upload_date = None
        ts = img.get("timestamp")
        if ts:
            try:
                upload_date = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                pass

        # 3a. Upsert photo
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
            db.flush()  # Get photo.id
            stats["new_photos"] += 1
        else:
            # Update mutable fields
            photo.description = description
            photo.thumb_url = thumb_url
            photo.categories = json.dumps(categories)

        # 3b. Diff usages
        current_api_usages = set()
        for u in detail.get("global_usage", []):
            key = (u.get("title", ""), u.get("wiki", ""))
            current_api_usages.add(key)

        # Existing active usages in DB for this photo
        existing_usages = {
            (u.article_title, u.wiki): u
            for u in photo.usages
            if u.is_active
        }

        # Find new usages
        for title, wiki in current_api_usages:
            stats["total_usages"] += 1
            if (title, wiki) in existing_usages:
                # Still active — update last_seen
                existing_usages[(title, wiki)].last_seen = now
            else:
                # Check if it was previously removed and is back
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

                # Log add event
                db.add(UsageEvent(
                    photo_id=photo.id,
                    article_title=title,
                    wiki=wiki,
                    event_type="added",
                    timestamp=now,
                ))
                stats["new_usages"] += 1

        # Find removed usages
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

    # 4. Update last_polled
    user.last_polled = now
    db.commit()

    return stats
