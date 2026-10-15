import io
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
import pytest
from PIL import Image
from sqlalchemy import create_engine, select

from app import create_app
from app.config import Config
from app.models import db, Post, MediaAsset, MediaTask, AnalyticsSession, SiteSetting
from app import media

@pytest.fixture
def app(tmp_path, monkeypatch):
    for key, value in {'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(tmp_path/'test.db'),'SQLALCHEMY_ENGINE_OPTIONS':{},'MEDIA_ROOT':str(tmp_path/'media'),'MEDIA_WORKER_ENABLED':False,'TESTING':True,'WTF_CSRF_ENABLED':False,'AUTO_SYNC_INTERVAL':0}.items():
        monkeypatch.setattr(Config,key,value,raising=False)
    app=create_app()
    with app.app_context():
        yield app
        db.session.remove()
        db.drop_all()


def photo(size=(2800,1800), fmt='JPEG'):
    out=io.BytesIO()
    Image.effect_noise(size,70).convert('RGB').save(out,fmt,quality=92)
    out.seek(0)
    return out


def enable_r2(app, monkeypatch):
    app.config.update(R2_ENDPOINT_URL='https://example.r2.cloudflarestorage.com', R2_ACCESS_KEY_ID='test',R2_SECRET_ACCESS_KEY='test',R2_BUCKET='news',R2_PUBLIC_URL='https://images.example.test')
    client=Mock()
    monkeypatch.setattr(media,'r2_client',lambda:client)
    return client


def test_compression_and_dedup(app):
    source=photo(); original=len(source.getvalue())
    first=media.save_upload(source,'camera.jpg','posts')
    second=media.save_upload(io.BytesIO(source.getvalue()),'camera.jpg','posts')
    assert first==second
    path=media.local_path(first.removeprefix('/media/'))
    with Image.open(path) as im:
        assert im.format=='WEBP' and max(im.size)<=1280
    assert path.stat().st_size < original * .4
    assert MediaAsset.query.count()==1


def test_limits_and_path_escape(app):
    app.config['IMAGE_MAX_BYTES']=100
    with pytest.raises(Exception):
        media.save_upload(io.BytesIO(b'x'*101),'large.jpg')
    with pytest.raises(Exception):
        media.local_path('../../secret')
    assert not list(media.root().rglob('*.webp'))


def test_pixels_rejected(app):
    app.config['IMAGE_MAX_PIXELS']=100
    with pytest.raises(Exception):
        media.save_upload(photo((11,11)),'large.jpg')


def test_animation_survives(app):
    out=io.BytesIO()
    Image.new('RGB',(30,30),'red').save(out,'GIF',save_all=True,append_images=[Image.new('RGB',(30,30),'blue')],duration=100,loop=0)
    out.seek(0)
    url=media.save_upload(out,'ad.gif','ads')
    with Image.open(media.local_path(url.removeprefix('/media/'))) as im:
        assert im.n_frames==2


def test_upload_does_not_commit_article_transaction(app):
    post=Post(title='Pending',slug='pending')
    db.session.add(post)
    media.save_upload(photo((50,50)),'small.jpg')
    db.session.rollback()
    assert Post.query.count()==0
    assert MediaAsset.query.count()==1


def test_r2_background_and_direct_delivery(app, monkeypatch):
    client=enable_r2(app,monkeypatch)
    url=media.save_upload(photo((400,300)),'cover.jpg')
    assert url.startswith('/media/') and not client.upload_file.called
    assert media.run_one()
    assert client.upload_file.call_count==1
    final=media.resolve_media(url)
    assert final.startswith(app.config['R2_PUBLIC_URL'])
    args=client.upload_file.call_args.kwargs
    assert 'immutable' in args['ExtraArgs']['CacheControl']
    response=app.test_client().get(url)
    assert response.status_code==302 and response.location==final
    post=Post(title='Cloud',slug='cloud',featured_image=url,published_at=datetime.utcnow())
    db.session.add(post); db.session.commit()
    body=app.test_client().get('/').get_data(as_text=True)
    assert final in body
    assert 'src="'+url+'"' not in body


def test_migrate_preserves_original_and_local_fallback(app):
    old=media.local_path('old/cover.jpg'); old.parent.mkdir(); old.write_bytes(photo((1800,1300)).getvalue())
    original=old.read_bytes()
    list(media.index_existing())
    assert media.run_one()
    target=media.resolve_media('/media/old/cover.jpg')
    assert target.endswith('.webp')
    assert old.read_bytes()==original
    assert app.test_client().get('/media/old/cover.jpg').status_code==302
    assert app.test_client().get(target).status_code==200


def test_migrate_to_r2_preserves_original(app,monkeypatch):
    client=enable_r2(app,monkeypatch)
    old=media.local_path('old.jpg'); old.write_bytes(photo((1800,1300)).getvalue())
    list(media.index_existing()); media.run_one()
    assert old.exists() and media.resolve_media('/media/old.jpg').startswith('https://images.')
    assert client.upload_file.called


def test_persistent_retry_leaves_image_visible(app,monkeypatch):
    post=Post(title='Retry',slug='retry',featured_image='https://publisher.test/a.jpg',published_at=datetime.utcnow())
    db.session.add(post);db.session.commit();media.enqueue_post(post.id)
    monkeypatch.setattr(media,'remote_image',Mock(side_effect=TimeoutError()))
    assert media.run_one()
    task=MediaTask.query.one()
    assert task.state=='pending' and task.attempts==1 and task.error=='TimeoutError'
    assert db.session.get(Post,post.id).featured_image=='https://publisher.test/a.jpg'
    assert not media.run_one()


def test_post_images_share_cache_and_remove_srcset(app,monkeypatch):
    post=Post(title='Mirror',slug='mirror',featured_image='https://source.test/a.jpg',content_html='<img src="https://source.test/a.jpg" srcset="https://source.test/original.jpg 2x">')
    db.session.add(post);db.session.commit();post_id=post.id
    convert=Mock(return_value='/media/optimized.webp');monkeypatch.setattr(media,'remote_image',convert)
    media.process_post(post_id)
    post=db.session.get(Post,post_id)
    assert post.featured_image=='/media/optimized.webp'
    assert 'srcset' not in post.content_html and '/media/optimized.webp' in post.content_html
    assert convert.call_count==1


def test_admin_pagination_and_pages(app):
    db.session.add_all([Post(title=f'Post {i}',slug=f'post-{i}',content_html='x'*5000) for i in range(85)]);db.session.commit()
    client=app.test_client()
    with client.session_transaction() as session:
        session['_user_id']='1';session['_fresh']=True
    for path in ['/admin/','/admin/posts','/admin/posts?page=2','/admin/media','/admin/insights','/admin/settings','/admin/categories','/admin/wordpress']:
        response=client.get(path)
        assert response.status_code==200, (path,response.get_data(as_text=True)[:200])
    response=client.get('/admin/posts')
    assert response.get_data(as_text=True).count('Excluir matéria</button>')==40
    assert client.get('/admin/api/resources').json['r2_enabled'] is False
    client.get('/admin/logout')
    assert client.get('/admin/api/resources').status_code==401


def test_stats_sql(app):
    from app.admin import _analytics_stats
    db.session.add_all([AnalyticsSession(session_id='a',visitor_id='v',pageviews=3,duration_seconds=60,is_bounce=False,is_new_user=True),AnalyticsSession(session_id='b',visitor_id='v',pageviews=1,duration_seconds=0,is_bounce=True,is_new_user=False)])
    db.session.commit()
    result=_analytics_stats()
    assert result['current']==dict(sessions=2,pageviews=4,total_users=1,avg_duration=30,bounce_rate=50.0,new_users=1)
    assert result['daily_series'][-1]['total_users']==1


def test_hub_returns_before_download(app,monkeypatch):
    db.session.add(SiteSetting(key='hub_receive_token_test',value='unused'))
    setting=SiteSetting.query.filter_by(key='hub_receive_token').one();setting.value='test-token';db.session.commit()
    # Existing endpoint also requires hub_enabled.
    SiteSetting.query.filter_by(key='hub_enabled').one().value='1';db.session.commit()
    downloader=Mock(side_effect=AssertionError('network must be background'))
    monkeypatch.setattr(media,'remote_image',downloader)
    response=app.test_client().post('/api/hub/posts/upsert',headers={'X-Hub-Token':'test-token'},json={'post':{'slug':'mirror','title':'Mirror','featured_image':'https://source.test/large.jpg'}})
    assert response.status_code==200, response.json
    assert response.json['images_processing']
    assert MediaTask.query.filter_by(kind='post',state='pending').count()==1
    downloader.assert_not_called()


def test_postgres_driver():
    assert create_engine('postgresql+psycopg2://u:p@localhost/db').dialect.driver=='psycopg2'


def test_requeue_during_processing_survives(app,monkeypatch):
    post=Post(title='Latest edit',slug='latest')
    db.session.add(post); db.session.commit();post_id=post.id
    media.enqueue_post(post_id)
    monkeypatch.setattr(media,'process_post',lambda target:media.enqueue_post(target))
    media.run_one()
    assert MediaTask.query.one().state=='pending'


def test_home_does_not_load_article_bodies(app):
    from sqlalchemy import inspect
    from app.routes import _home_posts_query
    post=Post(title='Long',slug='long',content_html='a'*100000,published_at=datetime.utcnow())
    db.session.add(post);db.session.commit();db.session.expunge_all()
    with app.test_request_context('/'):
        post=_home_posts_query().first()
        assert 'content_html' in inspect(post).unloaded
        assert len(post.summary)==1000


def test_remote_download_is_bounded_and_cached(app,monkeypatch):
    payload=photo((500,300)).getvalue()
    response=Mock(); response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    response.iter_content.return_value=[payload]
    get=Mock(return_value=response);monkeypatch.setattr(media.requests,'get',get)
    url=media.remote_image('https://publisher.test/asset?id=123')
    assert url.endswith('.webp')
    assert media.remote_image('https://publisher.test/asset?id=123')==url
    assert get.call_count==1
    assert get.call_args.kwargs['stream'] is True
    app.config['IMAGE_MAX_BYTES']=100
    response.iter_content.return_value=[b'x'*101]
    with pytest.raises(ValueError):
        media.remote_image('https://publisher.test/too-large')


def test_old_local_alias_migrates_after_r2_activation(app,monkeypatch):
    old=media.local_path('old.jpg'); old.write_bytes(photo((800,600)).getvalue())
    list(media.index_existing());media.run_one()
    assert media.resolve_media('/media/old.jpg').startswith('/media/')
    enable_r2(app,monkeypatch)
    list(media.index_existing())
    while media.run_one():pass
    assert media.resolve_media('/media/old.jpg').startswith('https://images.example.test')


def test_newer_article_edit_is_not_overwritten(app,monkeypatch):
    post=Post(title='Race',slug='race',featured_image='https://old.test/a.jpg',content_html='<p>old</p>')
    db.session.add(post);db.session.commit();post_id=post.id
    def convert(_):
        with db.engine.begin() as conn:
            conn.execute(Post.__table__.update().where(Post.id==post_id).values(featured_image='https://new.test/b.jpg',content_html='<p>new</p>'))
        return '/media/old.webp'
    monkeypatch.setattr(media,'remote_image',convert)
    media.process_post(post_id)
    post=db.session.get(Post,post_id)
    assert post.featured_image=='https://new.test/b.jpg'
    assert post.content_html=='<p>new</p>'


def test_live_posts_have_priority_over_backfill(app,monkeypatch):
    media.enqueue('post', '1', background=True)
    media.enqueue('post', '2')
    processed=[]
    monkeypatch.setattr(media,'process_post',lambda target:processed.append(target))
    media.run_one()
    assert processed==['2']
    media.run_one()
    assert processed==['2','1']


def test_webp_mime_without_system_registry(app,monkeypatch):
    monkeypatch.setattr(media.mimetypes,'guess_type',lambda _: (None,None))
    client=enable_r2(app,monkeypatch)
    url=media.save_upload(photo((500,300)),'photo.jpg')
    response=app.test_client().get(url)
    assert response.mimetype=='image/webp'
    media.run_one()
    assert client.upload_file.call_args.kwargs['ExtraArgs']['ContentType']=='image/webp'
