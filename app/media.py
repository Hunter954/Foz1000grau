"""Bounded image processing and durable, single-consumer media delivery.

Publication stores the text immediately. Remote images retain their working source
until a background task replaces them. Originals are never removed by migration.
"""
import hashlib
import html
import io
import json
import mimetypes
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse, quote, unquote

import click
import requests
from flask import current_app, request, jsonify
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select, case
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from .models import db, MediaAsset, MediaTask, Post

_IMAGE_LOCK = threading.Lock()
IMAGE_SRC = re.compile(r'(<img\b[^>]*?\bsrc=["\'])([^"\']+)(["\'])', re.I)
MEDIA_LINK = re.compile(r'(?:(?:https?://[^\s"\'<>/]+))?/media/[^\s"\'<>?&#)]+')
IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tif', '.tiff', '.ico'}


def content_type(filename):
    # Python 3.11/minimal runtime images do not always ship a WebP MIME entry.
    known = {'.webp': 'image/webp', '.avif': 'image/avif', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.svg': 'image/svg+xml', '.ico': 'image/x-icon'}
    return known.get(Path(filename).suffix.lower()) or mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def root():
    return Path(current_app.config['MEDIA_ROOT']).resolve()


def local_path(relative):
    path = (root() / relative).resolve()
    if not path.is_relative_to(root()):
        raise BadRequest('Caminho de mídia inválido')
    return path


def r2_enabled():
    return all(current_app.config.get(k) for k in ('R2_ENDPOINT_URL', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET', 'R2_PUBLIC_URL'))


def r2_client():
    if 'r2_client' not in current_app.extensions:
        import boto3
        from botocore.config import Config
        current_app.extensions['r2_client'] = boto3.client('s3', endpoint_url=current_app.config['R2_ENDPOINT_URL'], aws_access_key_id=current_app.config['R2_ACCESS_KEY_ID'], aws_secret_access_key=current_app.config['R2_SECRET_ACCESS_KEY'], region_name='auto', config=Config(connect_timeout=4, read_timeout=15, max_pool_connections=2, retries={'max_attempts': 2, 'mode': 'standard'}))
    return current_app.extensions['r2_client']


def _asset(source):
    return db.session.execute(select(MediaAsset).where(MediaAsset.source_key == digest(source))).scalar_one_or_none()


def _remember(source, relative, original_bytes=0, public_url=None, size_bytes=None):
    # Independent transaction: never commits an unfinished editorial request.
    table = MediaAsset.__table__
    values = dict(source_key=digest(source), source_url=source, local_path=relative, size_bytes=size_bytes if size_bytes is not None else local_path(relative).stat().st_size, original_bytes=original_bytes, updated_at=datetime.utcnow())
    if public_url:
        values['public_url'] = public_url
    with db.engine.begin() as conn:
        found = conn.execute(select(table.c.id).where(table.c.source_key == values['source_key'])).scalar()
        if found:
            conn.execute(table.update().where(table.c.id == found).values(**values))
        else:
            conn.execute(table.insert().values(**values))


def enqueue(kind, target, background=False):
    table = MediaTask.__table__
    key = digest(kind + ':' + str(target))
    stored_kind = 'backfill-' + kind if background else kind
    try:
        with db.engine.begin() as conn:
            task = conn.execute(select(table.c.id).where(table.c.task_key == key)).scalar()
            if task:
                conn.execute(table.update().where(table.c.id == task).values(kind=stored_kind, state='pending', attempts=0, available_at=datetime.utcnow(), error=None))
            else:
                conn.execute(table.insert().values(kind=stored_kind, target=str(target), task_key=key, state='pending', attempts=0, available_at=datetime.utcnow()))
    except IntegrityError:
        pass  # Another request already queued the same durable work.


def enqueue_post(post_id):
    enqueue('post', post_id)


def _bounded_copy(source, output):
    count = 0
    while chunk := source.read(64 * 1024):
        count += len(chunk)
        if count > current_app.config['IMAGE_MAX_BYTES']:
            raise RequestEntityTooLarge('A imagem excede o limite de 15 MB.')
        output.write(chunk)
    return count


def optimize(source, filename):
    """One decode and one WebP encode. No expensive repeated quality search."""
    with _IMAGE_LOCK:
        with Image.open(source) as image:
            if image.width * image.height > current_app.config['IMAGE_MAX_PIXELS']:
                raise BadRequest('A imagem excede o limite de 24 megapixels.')
            # Animated ads must keep their animation. Bounded original is copied.
            if getattr(image, 'is_animated', False):
                source.seek(0)
                return source.read(), {'GIF': '.gif', 'WEBP': '.webp', 'PNG': '.png'}.get(image.format, Path(filename).suffix.lower() or '.gif')
            image.draft('RGB', (1280, 1280))
            image.thumbnail((current_app.config['IMAGE_MAX_EDGE'],) * 2, Image.Resampling.LANCZOS)
            image = ImageOps.exif_transpose(image)
            if image.mode not in ('RGB', 'RGBA'):
                image = image.convert('RGBA' if 'transparency' in image.info else 'RGB')
            output = io.BytesIO()
            image.save(output, 'WEBP', quality=current_app.config['IMAGE_QUALITY'], method=3)
            return output.getvalue(), '.webp'


def save_upload(stream, filename, subdir='general'):
    # Bounded spool avoids keeping an original phone photo and its pixels in RAM.
    with tempfile.TemporaryFile() as source:
        original_size = _bounded_copy(stream, source)
        source.seek(0)
        ext = Path(filename).suffix.lower()
        if ext in IMAGE_EXT:
            try:
                payload, ext = optimize(source, filename)
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
                raise BadRequest('Imagem inválida ou corrompida.') from exc
        else:
            # SVG and other existing media retain their original representation.
            payload = source.read()
        content_hash = hashlib.sha256(payload).hexdigest()
        # Immutable, content-addressed names avoid duplicate storage and cache busts.
        safe_folder = 'images' if ext == '.webp' else (re.sub(r'[^a-zA-Z0-9/_-]', '', subdir).strip('/') or 'general')
        relative = f'{safe_folder}/{content_hash}{ext or ".bin"}'
        if r2_enabled():
            key = 'foz1000grau/' + relative
            r2_client().put_object(Bucket=current_app.config['R2_BUCKET'], Key=key, Body=payload,
                ContentType=content_type(relative), CacheControl='public, max-age=31536000, immutable')
            public = current_app.config['R2_PUBLIC_URL'] + '/' + quote(key, safe='/')
            with requests.head(public, timeout=(4, 10), allow_redirects=True) as response:
                response.raise_for_status()
                if int(response.headers.get('Content-Length', -1)) != len(payload):
                    raise ValueError('public_size_mismatch')
            _remember('/media/' + relative, relative, original_size, public, size_bytes=len(payload))
            return public

        if current_app.config.get('R2_BUCKET'):
            raise RuntimeError('R2 configurado sem credenciais completas; upload local bloqueado')
        destination = local_path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as tmp:
                tmp.write(payload)
                temp_name = tmp.name
            os.replace(temp_name, destination)
    url = '/media/' + relative
    _remember(url, relative, original_size)
    return resolve_media(url)


def resolve_media(url):
    if not url:
        return url
    parsed = urlparse(url)
    # Do not confuse another publisher's /media path with our own files.
    if parsed.netloc and parsed.hostname not in {'foz1000grau.com.br', 'www.foz1000grau.com.br', 'foz1000grau.up.railway.app'}:
        return url
    if not parsed.path.startswith('/media/'):
        return url
    asset = _asset(unquote(parsed.path))
    return asset.public_url if asset and asset.public_url else url


def remote_image(url, folder='imported'):
    if not url:
        return url
    if url.startswith('/media/'):
        return resolve_media(url)
    if current_app.config.get('R2_PUBLIC_URL') and url.startswith(current_app.config['R2_PUBLIC_URL'] + '/'):
        return url
    cached = _asset(url)
    if cached:
        return cached.public_url or '/media/' + cached.local_path
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http'):
        return url
    with tempfile.TemporaryFile() as source:
        with requests.get(url, stream=True, timeout=(4, 15)) as response:
            response.raise_for_status()
            count = 0
            for chunk in response.iter_content(64 * 1024):
                count += len(chunk)
                if count > current_app.config['IMAGE_MAX_BYTES']:
                    raise ValueError('remote_image_too_large')
                source.write(chunk)
            source.seek(0)
            # Validate format from content even when remote URL has no extension.
            with Image.open(source) as im:
                ext = '.' + ('jpg' if im.format == 'JPEG' else im.format.lower())
            source.seek(0)
            local_url = save_upload(source, 'source' + ext, folder)
    relative = urlparse(local_url).path.removeprefix('/media/')
    if local_url.startswith('/media/'):
        _remember(url, relative, count)
        return local_url
    # An existing identical upload may already be on R2.
    with db.engine.begin() as conn:
        row = conn.execute(select(MediaAsset.__table__).where(MediaAsset.public_url == local_url)).mappings().first()
    if row:
        _remember(url, row['local_path'], count, local_url, size_bytes=row['size_bytes'])
    return local_url


def process_post(post_id):
    row = db.session.execute(select(Post.featured_image, Post.content_html).where(Post.id == int(post_id))).first()
    db.session.remove()
    if not row:
        return
    featured, body = row
    cache = {}
    def convert(src):
        src = html.unescape(src)
        if src not in cache:
            cache[src] = remote_image(src)
        return cache[src]
    new_featured = convert(featured) if featured else featured
    body_for_images = re.sub(r'(<img\b[^>]*?)\s+srcset=[\"\'][^\"\']*[\"\']', r'\1', body or '', flags=re.I)
    new_body = IMAGE_SRC.sub(lambda m: m[1] + html.escape(convert(m[2]), quote=True) + m[3], body_for_images) if body else body
    # Compare snapshots so a slow image task cannot overwrite a newer edit.
    table = Post.__table__
    with db.engine.begin() as conn:
        conn.execute(table.update().where(table.c.id == int(post_id), table.c.featured_image == featured).values(featured_image=new_featured))
        conn.execute(table.update().where(table.c.id == int(post_id), table.c.content_html == body).values(content_html=new_body))


def upload_asset(relative):
    if not r2_enabled():
        raise ValueError('r2_not_configured')
    source = '/media/' + relative
    path = local_path(relative)
    if not path.is_file():
        return
    asset = _asset(source)
    if asset and asset.public_url and asset.public_url.startswith('https://'):
        return
    # Upload the existing file directly; do not create another local derivative.
    relative_out = relative
    from boto3.s3.transfer import TransferConfig
    key = 'foz1000grau/' + relative_out
    r2_client().upload_file(str(local_path(relative_out)), current_app.config['R2_BUCKET'], key, ExtraArgs={'ContentType': content_type(relative_out), 'CacheControl': 'public, max-age=31536000, immutable'}, Config=TransferConfig(use_threads=False, max_concurrency=1))
    public = current_app.config['R2_PUBLIC_URL'] + '/' + quote(key, safe='/')
    _remember(source, relative, path.stat().st_size, public)
    # Remote-source aliases share the compressed path; resolve them to R2 too.
    with db.engine.begin() as conn:
        conn.execute(MediaAsset.__table__.update().where(MediaAsset.local_path == relative_out).values(public_url=public))


def optimize_legacy(relative):
    path = local_path(relative)
    if not path.is_file() or path.suffix.lower() not in IMAGE_EXT:
        return
    with path.open('rb') as fh:
        target = save_upload(fh, path.name, 'optimized')
    _remember('/media/' + relative, relative, path.stat().st_size, target)


def seed_existing_posts():
    cursor = 0
    while True:
        with db.engine.connect() as conn:
            ids = conn.execute(select(Post.id).where(Post.id > cursor).order_by(Post.id).limit(50)).scalars().all()
        if not ids:
            break
        for post_id in ids:
            with db.engine.connect() as conn:
                exists = conn.execute(select(MediaTask.id).where(MediaTask.task_key == digest('post:'+str(post_id)))).scalar()
            if not exists:
                enqueue('post', post_id, background=True)
            cursor = post_id
        run_one()
        db.session.remove()


def list_media(page, per_page):
    rows = db.session.execute(select(MediaAsset).where(MediaAsset.source_url.like('/media/%'), MediaAsset.hidden.is_(False)).order_by(MediaAsset.updated_at.desc(), MediaAsset.id.desc()).offset((page-1)*per_page).limit(per_page+1)).scalars().all()
    return [dict(name=Path(a.local_path).name, delete_url=a.source_url, url=a.public_url or a.source_url, size_kb=max(1, round(a.size_bytes/1024)), updated_at=a.updated_at) for a in rows[:per_page]], len(rows)>per_page


def delete_media(url):
    # Shared content hashes and original files stay available for old articles.
    # Hide only the catalog entry; never implicitly delete shared R2 objects.
    asset = _asset(urlparse(url).path if '/media/' in url else url)
    if asset:
        asset.hidden = True
        db.session.commit()


@contextmanager
def worker_lock():
    import fcntl
    with (root() / '.media-worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_one():
    task = db.session.execute(select(MediaTask).where(MediaTask.state == 'pending', MediaTask.available_at <= datetime.utcnow()).order_by(case((MediaTask.kind.like('backfill-%'), 1), else_=0), MediaTask.id).limit(1)).scalar_one_or_none()
    if not task:
        return False
    task_id, kind, target, attempts, revision = task.id, task.kind.removeprefix('backfill-'), task.target, task.attempts, task.available_at
    db.session.remove()
    try:
        if kind == 'post':
            process_post(target)
        elif kind == 'asset':
            upload_asset(target)
        elif kind == 'optimize':
            optimize_legacy(target)
        error = None
    except Exception as exc:
        error = type(exc).__name__  # Never log remote URLs, tokens or credentials.
    db.session.remove()
    with db.engine.begin() as conn:
        # A save during processing requeues work and must not be marked done.
        state = 'done' if error is None else ('failed' if attempts >= 4 else 'pending')
        conn.execute(MediaTask.__table__.update().where(MediaTask.id == task_id, MediaTask.available_at == revision).values(state=state, attempts=attempts+1, error=error, available_at=datetime.utcnow()+timedelta(seconds=min(3600,30*2**attempts))))
    return True


def index_existing():
    # Streaming directory walk, no list/sort of the entire volume in RAM.
    for directory, _, files in os.walk(root()):
        for filename in files:
            if filename.startswith('.'):
                continue
            path = Path(directory)/filename
            relative = path.relative_to(root()).as_posix()
            source = '/media/' + relative
            if not _asset(source):
                _remember(source, relative, path.stat().st_size)
            kind = 'asset' if r2_enabled() else 'optimize'
            if r2_enabled() or (len(path.stem) != 64 and path.suffix.lower() in IMAGE_EXT):
                with db.engine.connect() as conn:
                    exists = conn.execute(select(MediaTask.id).where(MediaTask.task_key == digest(kind+':'+relative))).scalar()
                if not exists:
                    enqueue(kind, relative, background=True)
            db.session.remove()
            yield relative


def _worker(app):
    # The OS lock also covers gunicorn overlap during rolling deployments.
    while True:
        try:
            with app.app_context(), worker_lock() as acquired:
                if acquired:
                    if not app.extensions.get('media_indexed'):
                        indexed = 0
                        for n, _ in enumerate(index_existing()):
                            indexed += 1
                            if n % 10 == 0:
                                run_one()
                            time.sleep(.01)
                        seed_existing_posts()
                        app.extensions['media_indexed'] = True
                        app.logger.warning('MEDIA_INDEX_READY files=%s r2=%s', indexed, r2_enabled())
                    did_work = run_one()
                else:
                    did_work = False
                db.session.remove()
        except Exception as exc:
            app.logger.warning('Media worker retry scheduled (%s)', type(exc).__name__)
            did_work = False
        time.sleep(1 if did_work else 5)


def init_media(app):
    @app.after_request
    def direct_cloud_urls(response):
        # Resolve links in one bounded query, avoiding an image proxy/redirect per view.
        if response.mimetype == 'text/html' and not response.direct_passthrough and not request.path.startswith('/admin'):
            text = response.get_data(as_text=True)
            links = set(MEDIA_LINK.findall(text))
            keys = {digest(unquote(urlparse(link).path)) for link in links if not urlparse(link).hostname or urlparse(link).hostname in {'foz1000grau.com.br','www.foz1000grau.com.br','foz1000grau.up.railway.app'}}
            if keys:
                rows = db.session.execute(select(MediaAsset.source_url, MediaAsset.public_url).where(MediaAsset.source_key.in_(list(keys)[:512]), MediaAsset.public_url.isnot(None))).all()
                mapping = dict(rows)
                text = MEDIA_LINK.sub(lambda m: mapping.get(unquote(urlparse(m[0]).path), m[0]) if not urlparse(m[0]).hostname or urlparse(m[0]).hostname in {'foz1000grau.com.br','www.foz1000grau.com.br','foz1000grau.up.railway.app'} else m[0], text)
                response.set_data(text)
        return response

    @app.get('/admin/api/resources')
    def media_resources():
        from flask_login import current_user
        if not current_user.is_authenticated or not current_user.is_admin:
            return jsonify(error='unauthorized'), 401
        from sqlalchemy import func
        counts = db.session.execute(select(MediaTask.state, func.count()).group_by(MediaTask.state)).all()
        totals = db.session.execute(select(func.sum(MediaAsset.size_bytes), func.sum(MediaAsset.original_bytes)).where(MediaAsset.source_url.like('/media/%'))).first()
        return jsonify(r2_enabled=r2_enabled(), queue=dict(counts), catalog_bytes=totals[0] or 0, catalog_source_bytes=totals[1] or 0)

    @app.cli.command('retry-media')
    def retry_media():
        with db.engine.begin() as conn:
            result = conn.execute(MediaTask.__table__.update().where(MediaTask.state == 'failed').values(state='pending', attempts=0, available_at=datetime.utcnow()))
        click.echo(f'Requeued {result.rowcount} tasks')

    @app.cli.command('migrate-media')
    @click.option('--process', is_flag=True, help='Process queued work serially after indexing.')
    def migrate_media(process):
        with worker_lock() as acquired:
            if not acquired:
                raise click.ClickException('Media worker is already running; queued jobs will be processed automatically.')
            count = sum(1 for _ in index_existing())
            click.echo(f'Indexed {count} files; originals retained. R2 configured: {r2_enabled()}')
            if process:
                while run_one():
                    pass

    if app.config.get('MEDIA_WORKER_ENABLED'):
        threading.Thread(target=_worker, args=(app,), daemon=True, name='media-worker').start()
