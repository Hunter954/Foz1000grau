import os

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///dev.db")
    if SQLALCHEMY_DATABASE_URI.startswith(("postgres://", "postgresql://")):
        SQLALCHEMY_DATABASE_URI = "postgresql+psycopg2://" + SQLALCHEMY_DATABASE_URI.split("://", 1)[1]
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_size": 4, "max_overflow": 2, "pool_timeout": 10, "pool_pre_ping": True} if SQLALCHEMY_DATABASE_URI.startswith("postgresql") else {}
    R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL", "")
    R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET = os.getenv("R2_BUCKET", "")
    R2_PUBLIC_URL = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
    MEDIA_WORKER_ENABLED = os.getenv("MEDIA_WORKER_ENABLED", "1") == "1"
    IMAGE_MAX_BYTES = 15 * 1024 * 1024
    IMAGE_MAX_PIXELS = 24_000_000
    IMAGE_MAX_EDGE = 1280
    IMAGE_QUALITY = 68


    WP_BASE_URL = os.getenv("WP_BASE_URL", "https://foz1000grau.com.br").rstrip("/")
    WP_PER_PAGE = int(os.getenv("WP_PER_PAGE", "20"))

    AUTO_SYNC_INTERVAL = int(os.getenv("AUTO_SYNC_INTERVAL", "0"))

    SITE_NAME = os.getenv("SITE_NAME", "News")
    LIVE_EMBED_TITLE = os.getenv("LIVE_EMBED_TITLE", "AO VIVO")

    MEDIA_ROOT = os.getenv("MEDIA_ROOT", "/data/uploads")
    MEDIA_URL_PREFIX = os.getenv("MEDIA_URL_PREFIX", "/media")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", str(32 * 1024 * 1024)))
