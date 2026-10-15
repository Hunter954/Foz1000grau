from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import mimetypes
import re
import uuid

import bleach
import requests
from flask import current_app
from slugify import slugify

from .models import db, Post, Category, DeletedWpPost
from .wp_client import WPClient

ALLOWED_TAGS = bleach.sanitizer.ALLOWED_TAGS.union({
    "p","br","hr","img","h1","h2","h3","h4","h5","h6","blockquote",
    "ul","ol","li","strong","em","a","span","div","figure","figcaption"
})
ALLOWED_ATTRS = dict(bleach.sanitizer.ALLOWED_ATTRIBUTES)
ALLOWED_ATTRS.update({
    "a": ["href","title","target","rel"],
    "img": ["src","alt","title","loading","width","height"],
    "div": ["class"],
    "span": ["class"],
    "figure": ["class"],
})

IMG_SRC_RE = re.compile(r"(<img\b[^>]*?\bsrc=[\"'])([^\"']+)([\"'])", re.IGNORECASE)


def _featured_img_from_embed(p: dict) -> str | None:
    try:
        media = p.get("_embedded", {}).get("wp:featuredmedia", [])
        if media and "source_url" in media[0]:
            return media[0]["source_url"]
    except Exception:
        return None
    return None


def _media_url(relative_path: str) -> str:
    prefix = current_app.config.get("MEDIA_URL_PREFIX", "/media").rstrip("/")
    return f"{prefix}/{relative_path.lstrip('/')}"


def _guess_extension(source_url: str, content_type: str = "") -> str:
    parsed = urlparse(source_url or "")
    ext = Path(parsed.path).suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"}:
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    if guessed in {".jpe", ".jpeg"}:
        return ".jpg"
    if guessed in {".jpg", ".png", ".webp", ".gif", ".svg"}:
        return guessed
    return ".jpg"


def download_external_image(source_url: str | None, folder: str = "wp") -> str | None:
    from .media import remote_image
    return remote_image(source_url, folder)


def localize_content_images(html: str | None) -> str | None:
    if not html:
        return html

    cache = {}

    def repl(match):
        prefix, src, suffix = match.groups()
        if not src or src.startswith("/media/") or src.startswith("data:"):
            return match.group(0)
        if src not in cache:
            try:
                cache[src] = download_external_image(src, folder="wp/content") or src
            except Exception:
                cache[src] = src
        return f"{prefix}{cache[src]}{suffix}"

    return IMG_SRC_RE.sub(repl, html)


def sync_categories(client: WPClient):
    page = 1
    while True:
        data, _headers = client.list_categories(page=page, per_page=100)
        if not data:
            break

        for c in data:
            slug = c.get("slug") or slugify(c.get("name","cat"))
            cat = Category.query.filter_by(wp_id=c["id"]).first()
            if not cat:
                cat = Category(wp_id=c["id"], slug=slug, name=c.get("name",""))
                db.session.add(cat)
            else:
                cat.slug = slug
                cat.name = c.get("name","")

        db.session.commit()
        if len(data) < 100:
            break
        page += 1


def sync_posts(client: WPClient, max_pages: int = 10, per_page: int = 20, download_images: bool = False):
    page = 1
    while page <= max_pages:
        data, headers = client.list_posts(page=page, per_page=per_page)
        if not data:
            break

        deleted_wp_ids = {row[0] for row in db.session.query(DeletedWpPost.wp_id).filter(DeletedWpPost.wp_id.in_([p["id"] for p in data])).all()}

        changed_posts = []
        for p in data:
            wp_id = p["id"]
            if wp_id in deleted_wp_ids:
                continue
            title = (p.get("title") or {}).get("rendered") or ""
            slug = p.get("slug") or slugify(title)[:200]
            excerpt = (p.get("excerpt") or {}).get("rendered") or ""
            content = (p.get("content") or {}).get("rendered") or ""
            featured = _featured_img_from_embed(p)
            date_str = p.get("date_gmt") or p.get("date")
            mod_str = p.get("modified_gmt") or p.get("modified")
            published_at = datetime.fromisoformat(date_str.replace("Z","")) if date_str else None
            updated_at = datetime.fromisoformat(mod_str.replace("Z","")) if mod_str else None

            excerpt_safe = bleach.clean(excerpt, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
            content_safe = bleach.clean(content, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
            post = Post.query.filter_by(wp_id=wp_id).first()
            if not post:
                post = Post(wp_id=wp_id, source="wp", slug=slug, title=title)
                db.session.add(post)

            post.title = title
            post.slug = slug
            post.excerpt = excerpt_safe
            post.content_html = content_safe
            post.featured_image = featured
            post.published_at = published_at
            post.updated_at = updated_at

            changed_posts.append(post)
            post.categories = []
            for cid in (p.get("categories") or []):
                cat = Category.query.filter_by(wp_id=cid).first()
                if cat:
                    post.categories.append(cat)

        db.session.commit()
        from .media import enqueue_post
        for changed_post in changed_posts:
            enqueue_post(changed_post.id)
        total_pages = int(headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1


def localize_existing_wp_images(limit: int | None = None) -> dict:
    from .media import enqueue_post
    queued = 0
    cursor = 0
    while not limit or queued < limit:
        size = min(50, limit - queued) if limit else 50
        ids = db.session.query(Post.id).filter(Post.source == 'wp', Post.id > cursor).order_by(Post.id).limit(size).all()
        if not ids:
            break
        for (post_id,) in ids:
            enqueue_post(post_id)
            cursor = post_id
            queued += 1
        db.session.remove()
    return {'updated_posts': queued, 'featured_downloaded': 0, 'content_updated': 0, 'queued_posts': queued}
