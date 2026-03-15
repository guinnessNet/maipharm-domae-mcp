"""HMP 크롤러

특이사항:
- DWR(Direct Web Remoting) 프로토콜 기반
- 일반 HTML form이 아닌 DWR 호출로 데이터 조회
- 주문 기능 미구현
"""

from domae_mcp.core.crawlers.base import (
    BaseCrawler,
    CrawlerError,
    SearchResult,
)

# TODO: 실제 도메인으로 교체
BASE_URL = "https://www.hmpmall.co.kr"
LOGIN_URL = f"{BASE_URL}/member/login_proc.asp"
# DWR 엔드포인트: 검색은 DWR 프로토콜로 호출
DWR_URL = f"{BASE_URL}/dwr/call/plaincall/"


class HmpMallCrawler(BaseCrawler):
    """HMP 도매 크롤러 (DWR 프로토콜).

    - 로그인: 표준 form POST
    - 검색: DWR(Direct Web Remoting) 프로토콜 호출
      - DWR은 Java 서버의 메서드를 JavaScript에서 호출하는 프레임워크
      - HTTP POST로 특수 포맷의 요청 전송, 커스텀 응답 파싱 필요
    - 주문: 미구현 (복잡한 DWR 주문 프로세스)
    """

    SUPPLIER_NAME = "HMP"

    def __init__(self):
        super().__init__()
        # DWR 세션 파라미터
        self._http_session_id = ""
        self._script_session_id = ""
        self._batch_id = 0

    def login(self, login_id: str, login_pw: str) -> bool:
        """HMP 로그인.

        표준 form POST 로그인 후, DWR 세션 ID를 초기화.
        """
        try:
            payload = {
                "user_id": login_id,
                "user_pw": login_pw,
            }
            resp = self.session.post(LOGIN_URL, data=payload)
            resp.raise_for_status()

            # TODO: 로그인 성공 판별
            if "logout" in resp.text.lower() or "로그아웃" in resp.text:
                self._logged_in = True
                # DWR 세션 초기화
                self._init_dwr_session()
                return True

            return False
        except Exception as e:
            raise CrawlerError(f"[HMP] 로그인 실패: {e}") from e

    def _init_dwr_session(self):
        """DWR 세션 초기화.

        DWR 엔진 페이지를 로드하여 세션 파라미터를 추출.
        """
        try:
            # TODO: DWR 엔진 초기화 URL 확인
            resp = self.session.get(f"{BASE_URL}/dwr/engine.js")
            # httpSessionId, scriptSessionId 등을 응답에서 파싱
            # TODO: 실제 파싱 로직 구현
            self._http_session_id = ""
            self._script_session_id = ""
        except Exception:
            # DWR 초기화 실패해도 로그인 자체는 유지
            pass

    def _build_dwr_request(self, class_name: str, method_name: str,
                           params: list[str]) -> str:
        """DWR 요청 본문 생성.

        DWR 프로토콜 형식:
        callCount=1
        page=/xxx
        httpSessionId=xxx
        scriptSessionId=xxx
        c0-scriptName=ClassName
        c0-methodName=methodName
        c0-id=0
        c0-param0=string:value
        batchId=0
        """
        self._batch_id += 1
        lines = [
            "callCount=1",
            f"page={BASE_URL}/",
            f"httpSessionId={self._http_session_id}",
            f"scriptSessionId={self._script_session_id}",
            f"c0-scriptName={class_name}",
            f"c0-methodName={method_name}",
            "c0-id=0",
        ]
        for i, param in enumerate(params):
            lines.append(f"c0-param{i}=string:{param}")
        lines.append(f"batchId={self._batch_id}")
        return "\n".join(lines)

    def _parse_dwr_response(self, text: str) -> list[dict]:
        """DWR 응답 파싱.

        DWR 응답은 JavaScript 콜백 형태:
        //#DWR-INSERT
        //#DWR-REPLY
        var s0=...;
        dwr.engine._remoteHandleCallback('batch_id','0',s0);

        TODO: 실제 응답 형식에 맞춰 파싱 로직 구현
        """
        results: list[dict] = []
        # DWR 응답에서 데이터 추출
        # TODO: 정규식 또는 문자열 파싱으로 객체 배열 추출
        return results

    def search(self, keyword: str) -> list[SearchResult]:
        """HMP 검색 (DWR 프로토콜).

        DWR 호출로 검색 메서드를 실행하고 결과를 파싱.
        """
        try:
            # DWR 검색 요청 생성
            # TODO: 실제 클래스명/메서드명 확인
            dwr_body = self._build_dwr_request(
                class_name="ProductService",
                method_name="searchProduct",
                params=[keyword],
            )

            headers = {
                "Content-Type": "text/plain",
            }
            resp = self.session.post(
                f"{DWR_URL}ProductService.searchProduct.dwr",
                data=dwr_body,
                headers=headers,
            )
            resp.raise_for_status()

            # DWR 응답 파싱
            raw_results = self._parse_dwr_response(resp.text)
            results: list[SearchResult] = []

            for item in raw_results:
                results.append(SearchResult(
                    maker=item.get("maker", ""),
                    product_name=item.get("product_name", ""),
                    unit=item.get("unit", ""),
                    insurance_code=item.get("insurance_code", ""),
                    quantity=self._safe_int(str(item.get("quantity", 0))),
                    price=self._safe_int(str(item.get("price", 0))),
                    supplier=self.SUPPLIER_NAME,
                    product_id=item.get("product_id", ""),
                ))

            return results
        except CrawlerError:
            raise
        except Exception as e:
            raise CrawlerError(f"[HMP] 검색 실패: {e}") from e

    # order()는 미구현 — BaseCrawler 기본 동작(주문 미지원) 사용
