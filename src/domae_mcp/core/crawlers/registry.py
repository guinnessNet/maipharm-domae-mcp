"""크롤러 레지스트리: 도매상별 크롤러 등록 및 조회"""

from domae_mcp.core.crawlers.base import BaseCrawler


class CrawlerRegistry:
    """싱글턴 크롤러 레지스트리"""

    _instance = None
    _crawlers: dict[str, type[BaseCrawler]] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._crawlers = {}
        return cls._instance

    @classmethod
    def register(cls, name: str, crawler_class: type[BaseCrawler]):
        cls._crawlers[name] = crawler_class

    @classmethod
    def get(cls, name: str) -> BaseCrawler:
        if name not in cls._crawlers:
            raise KeyError(f"등록되지 않은 도매상: {name}")
        return cls._crawlers[name]()

    @classmethod
    def list_all(cls) -> list[str]:
        return list(cls._crawlers.keys())

    @classmethod
    def get_all(cls) -> dict[str, BaseCrawler]:
        return {name: cls() for name, cls in cls._crawlers.items()}


def _register_all():
    """모든 크롤러 자동 등록"""
    from domae_mcp.core.crawlers.geoweb import GeoWebCrawler
    from domae_mcp.core.crawlers.boksan import BoksanCrawler
    from domae_mcp.core.crawlers.inchun import InchunCrawler
    from domae_mcp.core.crawlers.tjpharm import TjPharmCrawler
    from domae_mcp.core.crawlers.hmpmall import HmpMallCrawler
    from domae_mcp.core.crawlers.beakje import BeakjeCrawler
    from domae_mcp.core.crawlers.picomall import PicomallCrawler
    from domae_mcp.core.crawlers.saeropharm import SaeropharmCrawler
    from domae_mcp.core.crawlers.sdpharm import SdpharmCrawler
    from domae_mcp.core.crawlers.upharmmall import UpharmmallCrawler

    CrawlerRegistry.register("지오영", GeoWebCrawler)
    CrawlerRegistry.register("복산", BoksanCrawler)
    CrawlerRegistry.register("인천", InchunCrawler)
    CrawlerRegistry.register("티제이팜", TjPharmCrawler)
    CrawlerRegistry.register("HMP", HmpMallCrawler)
    CrawlerRegistry.register("백제", BeakjeCrawler)
    CrawlerRegistry.register("피코", PicomallCrawler)
    CrawlerRegistry.register("새로팜", SaeropharmCrawler)
    CrawlerRegistry.register("신덕팜", SdpharmCrawler)
    CrawlerRegistry.register("대전동원약품", UpharmmallCrawler)


_register_all()
