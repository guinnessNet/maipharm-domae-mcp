"""크롤러 레지스트리: 동적 로드 방식

로컬 앱은 도매 v2 게이트(domae_mcp.v2.gate.RuntimeGate)가 검증·로드한 크롤러를 등록한다.
SearchService, OrderService는 CrawlerRegistry.get(name)으로 크롤러를 가져옴.
guard 가 설정돼 있으면 require()·get·get_all·list_all 이 먼저 guard 를 부른다(스펙 B5 모든 진입점, P9).
클라우드 워커는 guard 를 설정하지 않으므로 지금과 같다.
"""

import logging
from typing import Callable

from domae_mcp.core.crawlers.base import BaseCrawler

logger = logging.getLogger(__name__)


class CrawlerBlocked(Exception):
    """v2 게이트가 크롤러 실행을 막았다. reason 은 사유 코드다."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class CrawlerRegistry:
    """크롤러 레지스트리.

    v2 게이트의 publish() → register_all()로 동적 등록
    """

    _crawlers: dict[str, type[BaseCrawler]] = {}
    _loaded: bool = False
    _guard: Callable[[], None] | None = None

    @classmethod
    def set_guard(cls, fn: Callable[[], None] | None) -> None:
        """None 이면 guard 없음(클라우드·시험)."""
        cls._guard = fn

    @classmethod
    def require(cls) -> None:
        """서비스 진입 검사. 막히면 CrawlerBlocked."""
        guard = cls._guard
        if guard is not None:
            guard()

    @classmethod
    def register(cls, name: str, crawler_class: type[BaseCrawler]) -> None:
        """크롤러 등록."""
        cls._crawlers[name] = crawler_class
        logger.debug("크롤러 등록: %s", name)

    @classmethod
    def register_all(cls, crawlers: dict[str, type[BaseCrawler]]) -> None:
        """검증·로드한 크롤러를 일괄 등록."""
        cls._crawlers = dict(crawlers)
        cls._loaded = True
        logger.info("크롤러 %d개 일괄 등록", len(crawlers))

    @classmethod
    def get(cls, name: str) -> BaseCrawler:
        """크롤러 인스턴스 생성. 없으면 KeyError."""
        cls.require()
        if name not in cls._crawlers:
            raise KeyError(f"등록되지 않은 크롤러: {name}")
        return cls._crawlers[name]()

    @classmethod
    def list_all(cls) -> list[str]:
        """등록된 크롤러 이름 목록."""
        cls.require()
        return list(cls._crawlers.keys())

    @classmethod
    def get_all(cls) -> dict[str, BaseCrawler]:
        """모든 크롤러 인스턴스 생성."""
        cls.require()
        return {name: cls._crawlers[name]() for name in cls._crawlers}

    @classmethod
    def count(cls) -> int:
        """등록된 크롤러 수(guard 를 부르지 않는다, 상태 표시용)."""
        return len(cls._crawlers)

    @classmethod
    def is_loaded(cls) -> bool:
        """크롤러가 로드되었는지 여부."""
        return cls._loaded

    @classmethod
    def clear(cls) -> None:
        """등록된 크롤러 초기화 (철회·테스트용)."""
        cls._crawlers = {}
        cls._loaded = False
