"""설정 관리: ~/.maipharm-domae-mcp/ 기반 config.json + Fernet 암호화"""

import json
from pathlib import Path

from cryptography.fernet import Fernet


# 지원 도매상 목록
SUPPLIERS = ["지오영", "복산", "인천", "티제이팜", "HMP", "백제", "피코", "새로팜", "신덕팜", "대전동원약품", "경동사", "도현팜", "삼성팜", "훼미리팜"]

# 기본 설정
DEFAULT_CONFIG = {
    "version": "1.0.0",
    "port": 5900,
    "credentials": {},
    "telegram": {
        "token": "",
        "chat_id": "",
    },
    "api_key": "",
}


class ConfigManager:
    """로컬 설정 관리자.

    설정 경로: ~/.maipharm-domae-mcp/
    - config.json: 도매 계정(암호화), 텔레그램 설정, API 키, 포트 등
    - .key: Fernet 암호화 키 (자동 생성)
    """

    def __init__(self, base_dir: Path | None = None):
        self._base_dir = base_dir or Path.home() / ".maipharm-domae-mcp"
        self._config_path = self._base_dir / "config.json"
        self._key_path = self._base_dir / ".key"

        # 디렉토리 생성
        self._base_dir.mkdir(parents=True, exist_ok=True)

        # 암호화 키 로드 또는 생성
        self._fernet = Fernet(self._load_or_create_key())

        # config.json 로드 또는 초기화
        self._config = self._load_or_create_config()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    # ── 암호화 키 관리 ──

    def _load_or_create_key(self) -> bytes:
        """Fernet 키를 로드하거나 새로 생성"""
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self._key_path.write_bytes(key)
        # 소유자만 읽기/쓰기 가능하도록 권한 설정
        try:
            self._key_path.chmod(0o600)
        except OSError:
            pass  # Windows에서는 chmod 동작이 다를 수 있음
        return key

    def _encrypt(self, text: str) -> str:
        """문자열 암호화 → base64 문자열"""
        if not text:
            return ""
        return self._fernet.encrypt(text.encode("utf-8")).decode("utf-8")

    def _decrypt(self, token: str) -> str:
        """암호화된 문자열 복호화"""
        if not token:
            return ""
        return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")

    # ── config.json 관리 ──

    def _load_or_create_config(self) -> dict:
        """config.json 로드 또는 기본값으로 생성"""
        if self._config_path.exists():
            with open(self._config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        self._save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

    def _save_config(self, config: dict | None = None) -> None:
        """config.json 저장"""
        data = config if config is not None else self._config
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save(self) -> None:
        """현재 설정 저장 (내부 헬퍼)"""
        self._save_config()

    # ── 도매 계정 (credentials) ──

    def get_credentials(self, supplier: str) -> dict:
        """특정 도매상 계정 조회.

        Returns:
            {"login_id": "...", "login_pw": "..."} 또는
            {"login_id": "", "login_pw": ""} (미설정)
        """
        creds = self._config.get("credentials", {}).get(supplier)
        if not creds:
            return {"login_id": "", "login_pw": ""}
        return {
            "login_id": creds.get("login_id", ""),
            "login_pw": self._decrypt(creds.get("login_pw", "")),
        }

    def set_credentials(self, supplier: str, login_id: str, login_pw: str) -> None:
        """도매상 계정 저장 (비밀번호는 암호화)"""
        if "credentials" not in self._config:
            self._config["credentials"] = {}
        self._config["credentials"][supplier] = {
            "login_id": login_id,
            "login_pw": self._encrypt(login_pw),
        }
        self._save()

    def get_all_credentials(self) -> list[dict]:
        """전체 도매 계정 목록 조회 (비밀번호 마스킹).

        Returns:
            [
                {"supplier": "지오영", "login_id": "user1", "login_pw": "****", "configured": True},
                {"supplier": "복산", "login_id": "", "login_pw": "", "configured": False},
                ...
            ]
        """
        result = []
        for supplier in SUPPLIERS:
            creds = self._config.get("credentials", {}).get(supplier)
            if creds and creds.get("login_id"):
                result.append({
                    "supplier": supplier,
                    "login_id": creds["login_id"],
                    "login_pw": "****",
                    "configured": True,
                })
            else:
                result.append({
                    "supplier": supplier,
                    "login_id": "",
                    "login_pw": "",
                    "configured": False,
                })
        return result

    # ── 텔레그램 설정 ──

    def get_telegram(self) -> dict:
        """텔레그램 설정 조회.

        Returns:
            {"token": "...", "chat_id": "..."}
        """
        tg = self._config.get("telegram", {})
        return {
            "token": tg.get("token", ""),
            "chat_id": tg.get("chat_id", ""),
        }

    def set_telegram(self, token: str, chat_id: str) -> None:
        """텔레그램 설정 저장"""
        self._config["telegram"] = {
            "token": token,
            "chat_id": chat_id,
        }
        self._save()

    # ── API 키 ──

    def get_api_key(self) -> str | None:
        """API 키 조회"""
        key = self._config.get("api_key", "")
        return key if key else None

    def set_api_key(self, key: str) -> None:
        """API 키 저장"""
        self._config["api_key"] = key
        self._save()

    # ── 포트 ──

    def get_port(self) -> int:
        """웹서버 포트 조회"""
        return self._config.get("port", 5900)

    def set_port(self, port: int) -> None:
        """웹서버 포트 저장"""
        self._config["port"] = port
        self._save()
