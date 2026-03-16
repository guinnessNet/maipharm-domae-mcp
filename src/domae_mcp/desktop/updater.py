"""GitHub Release 기반 업데이트 체커"""

import logging
import requests
from domae_mcp import __version__

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com/repos/guinnessNet/maipharm-domae-mcp/releases/latest"


def check_for_update() -> dict | None:
    """최신 릴리스 확인. 업데이트 있으면 {tag, url} 반환, 없으면 None."""
    try:
        resp = requests.get(GITHUB_API, timeout=5)
        if resp.status_code != 200:
            return None

        data = resp.json()
        latest_tag = data.get("tag_name", "")  # "v1.0.1"
        current = f"v{__version__}"  # "v1.0.0"

        if latest_tag and latest_tag != current:
            return {
                "tag": latest_tag,
                "url": data.get("html_url", GITHUB_API),
            }
        return None
    except Exception as e:
        logger.debug("업데이트 체인 실패: %s", e)
        return None
