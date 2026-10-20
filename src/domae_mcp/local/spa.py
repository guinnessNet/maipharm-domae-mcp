"""React 앱 정적 파일 서빙: 화면 경로(/settings 등) 새로고침에도 index.html 을 준다"""

from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as e:
            # 확장자가 있는 파일(자산)과 /api 아래는 그대로 404 — 화면 경로만 앱으로 넘긴다.
            # Windows 에서는 path 구분자가 '\\' 로 온다
            parts = path.replace("\\", "/").split("/")
            if e.status_code != 404 or parts[0] == "api" or "." in parts[-1]:
                raise
            return await super().get_response("index.html", scope)
