"""업데이트 체크 라우터: GitHub Releases API로 최신 버전 확인"""

import logging
from typing import Optional

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from domae_mcp.local.config import ConfigManager

router = APIRouter(tags=["업데이트"])

logger = logging.getLogger(__name__)

_config = ConfigManager()

GITHUB_REPO = "guinnessNet/maipharm-domae-mcp"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


# ── Response 스키마 ──


class UpdateCheckResponse(BaseModel):
    current_version: str
    latest_version: str
    update_available: bool
    release_url: Optional[str] = None


# ── 엔드포인트 ──


@router.get("/update-check", response_model=UpdateCheckResponse)
def check_update():
    """GitHub Releases API로 최신 버전 체크."""
    current_version = _config._config.get("version", "1.0.0")

    try:
        resp = requests.get(
            GITHUB_API_URL,
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        latest_tag = data.get("tag_name", "")
        # 'v1.0.0' → '1.0.0'
        latest_version = latest_tag.lstrip("v")
        release_url = data.get("html_url", "")

        update_available = _compare_versions(current_version, latest_version)

        return UpdateCheckResponse(
            current_version=current_version,
            latest_version=latest_version,
            update_available=update_available,
            release_url=release_url,
        )

    except requests.RequestException as e:
        logger.warning("GitHub API 요청 실패: %s", str(e))
        raise HTTPException(
            status_code=502,
            detail="GitHub에서 버전 정보를 가져올 수 없습니다.",
        )


def _compare_versions(current: str, latest: str) -> bool:
    """시맨틱 버전 비교. latest가 더 높으면 True."""
    try:
        current_parts = [int(x) for x in current.split(".")]
        latest_parts = [int(x) for x in latest.split(".")]

        # 부족한 부분 0으로 패딩
        while len(current_parts) < 3:
            current_parts.append(0)
        while len(latest_parts) < 3:
            latest_parts.append(0)

        return latest_parts > current_parts
    except (ValueError, AttributeError):
        return False
