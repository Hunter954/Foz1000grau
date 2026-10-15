import requests
import json
from urllib.parse import urljoin

class WPClient:
    def __init__(self, base_url: str, timeout: int = 20):
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout = timeout

    def _get(self, path: str, params: dict):
        url = urljoin(self.base_url, path.lstrip("/"))
        with requests.get(url, params=params, timeout=(4, self.timeout), stream=True) as r:
            r.raise_for_status()
            body = bytearray()
            for chunk in r.iter_content(65536):
                body.extend(chunk)
                if len(body) > 4 * 1024 * 1024:
                    raise ValueError("WordPress response exceeds 4 MB")
            return json.loads(body), dict(r.headers)

    def list_posts(self, page: int = 1, per_page: int = 20):
        return self._get(
            "/wp-json/wp/v2/posts",
            {
                "page": page,
                "per_page": per_page,
                "_embed": 1,
                "orderby": "date",
                "order": "desc",
                "status": "publish",
            },
        )

    def list_categories(self, page: int = 1, per_page: int = 100):
        return self._get(
            "/wp-json/wp/v2/categories",
            {"page": page, "per_page": per_page, "hide_empty": True},
        )
