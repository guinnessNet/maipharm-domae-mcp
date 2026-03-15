"""크롤러 레지스트리: 동적 로드 방식

CrawlerLoader에서 로드한 크롤러를 관리한다.
SearchService, OrderService는 CrawlerRegistry.get(name)으로 크롤러를 가져옴.
"""

import logging

from domae_mcp.core.crawlers.base import BaseCrawler

logger = logging.getLogger(__name__)


class CrawlerRegistry:
    """크롤러 레지스트리.

    기존: _register_all()로 정적 import (모듈 로드 시 즉시 실행)
    변경: CrawlerLoader.load() → register_all()로 동적 등록
    """

    _crawlers: dict[str, type[BaseCrawler]] = {}
    _loaded: bool = False

    @classmethod
    def register(cls, name: str, crawler_class: type[BaseCrawler]) -> None:
        """크롤러 등록."""
        cls._crawlers[name] = crawler_class
        logger.debug("크롤러 등록: %s", name)

    @classmethod
    def register_all(cls, crawlers: dict[str, type[BaseCrawler]]) -> None:
        """CrawlerLoader에서 로드한 크롤러를 일괄 등록."""
        cls._crawlers = crawlers
        cls._loaded = True
        logger.info("크롤러 %d개 일괄 등록", len(crawlers))

    @classmethod
    def get(cls, name: str) -> BaseCrawler:
        """크롤러 인스턴스 생성. 없으면 KeyError."""
        if name not in cls._crawlers:
            raise KeyError(f"등록되지 않은 크롤러: {name}")
        return cls._crawlers[name]()

    @classmethod
    def list_all(cls) -> list[str]:
        """등록된 크롤러 이름 목록."""
        return list(cls._crawlers.keys())

    @classmethod
    def get_all(cls) -> dict[str, BaseCrawler]:
        """모든 크롤러 인스턴스 생성."""
        return {name: cls._crawlers[name]() for name in cls._crawlers}

    @classmethod
    def is_loaded(cls) -> bool:
        """크롤러가 로드되었는지 여부."""
        return cls._loaded

    @classmethod
    def clear(cls) -> None:
        """등록된 크롤러 초기화 (테스트용)."""
        cls._crawlers = {}
        cls._loaded = False
