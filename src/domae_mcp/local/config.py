"""설정 관리: 앱 홈(~/.maipharm-domae-mcp, MAIPHARM_DOMAE_HOME) 의 config.json + v2 보호 저장소

비밀(도매 비밀번호·텔레그램 토큰)은 v2/secrets.dpapi(CurrentUser DPAPI), API 키는 v2 키 슬롯에 둔다(스펙 B10, P16).
config.json 에는 비밀이 아닌 값(login_id, chat_id, 포트)만 남는다.
옛 형식(config.json 의 api_key·credentials.*.login_pw·telegram.token, .key)은 읽지 않고 지운 뒤 재입력을 요청한다.
"""

import json
import logging
from pathlib import Path

from domae_mcp.v2.keyslot import KeySlots
from domae_mcp.v2.runtime import build_kind
from domae_mcp.v2.store import SecretStore, app_home, path_lock
from domae_mcp.v2.winprotect import atomic_write_bytes, default_protector, quarantine

logger = logging.getLogger(__name__)


# 지원 도매상 목록
SUPPLIERS = ["지오영", "복산", "인천", "티제이팜", "HMP", "백제", "피코", "새로팜", "신덕팜", "대전동원약품", "경동사", "도현팜", "삼성팜", "훼미리팜"]

# 기본 설정(비밀 없음)
DEFAULT_CONFIG = {
    "version": "1.0.0",
    "port": 5900,
    "credentials": {},
    "telegram": {
        "chat_id": "",
    },
}

_warned_unprotected = False


class ConfigManager:
    """로컬 설정 관리자.

    설정 경로: 앱 홈(app_home())
    - config.json: 도매 login_id, 텔레그램 chat_id, 포트, 재입력 요청 목록
    - v2/secrets.dpapi: 도매 비밀번호·텔레그램 토큰
    - v2/keys·active.dpapi: API 키(설정의 키 검증 경로로만 바뀐다)
    """

    def __init__(self, base_dir: Path | None = None, protector=None):
        global _warned_unprotected
        self._base_dir = Path(base_dir) if base_dir else app_home()
        self._config_path = self._base_dir / "config.json"
        self._v2_root = self._base_dir / "v2"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._prot = protector or default_protector(build_kind())
        if not self._prot.protected and not _warned_unprotected:
            _warned_unprotected = True
            logger.warning("로컬 비밀이 보호되지 않습니다(개발용 source 실행)")
        self._secrets = SecretStore(self._v2_root, self._prot)
        self._lock = path_lock(self._config_path)
        self._config: dict = {}
        with self._lock:
            self._config = self._load_or_create_config()
            self._drop_legacy()

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    @property
    def protected(self) -> bool:
        return bool(self._prot.protected)

    # ── config.json 관리 ──

    def _read(self) -> dict:
        """config.json 을 매번 다시 읽는다(모듈마다 만든 인스턴스 사이 일관성). 손상이면 격리하고 기본값."""
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return self._config
        except OSError:  # 일시 오류는 마지막 값을 쓴다
            return self._config
        except ValueError:
            data = None
        if not isinstance(data, dict):
            quarantine(self._config_path)
            logger.warning("config.json 이 손상돼 격리하고 기본값으로 시작합니다")
            data = json.loads(json.dumps(DEFAULT_CONFIG))
            self._save_config(data)
        self._config = data
        return data

    def _load_or_create_config(self) -> dict:
        """config.json 로드 또는 기본값으로 생성"""
        if self._config_path.exists():
            return self._read()
        data = json.loads(json.dumps(DEFAULT_CONFIG))
        self._save_config(data)
        return data

    def _save_config(self, config: dict | None = None) -> None:
        """config.json 원자적 저장"""
        data = config if config is not None else self._config
        atomic_write_bytes(self._config_path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        self._config = data

    def _save(self) -> None:
        """현재 설정 저장 (내부 헬퍼)"""
        self._save_config()

    # ── 옛 형식 정리(P16) ──

    def _drop_legacy(self) -> None:
        cfg = self._read()
        reentry = list(cfg.get("reentry_required") or [])
        changed = False

        def need(item: str):
            if item not in reentry:
                reentry.append(item)

        if "api_key" in cfg:
            if cfg.get("api_key"):
                need("api_key")
            cfg.pop("api_key", None)
            changed = True
        if not isinstance(cfg.get("credentials"), dict):
            cfg["credentials"] = {}
            changed = True
        if "telegram" in cfg and not isinstance(cfg.get("telegram"), dict):
            cfg["telegram"] = {"chat_id": ""}
            changed = True
        for supplier, cred in cfg["credentials"].items():
            if isinstance(cred, dict) and "login_pw" in cred:
                if cred.get("login_pw"):
                    need(f"credentials:{supplier}")
                cred.pop("login_pw", None)
                changed = True
        tg = cfg.get("telegram")
        if isinstance(tg, dict) and "token" in tg:
            if tg.get("token"):
                need("telegram")
            tg.pop("token", None)
            changed = True
        key_file = self._base_dir / ".key"
        if changed or reentry != list(cfg.get("reentry_required") or []):
            cfg["reentry_required"] = reentry
            self._save_config(cfg)
        if key_file.exists():
            try:
                key_file.unlink()
            except OSError:
                logger.warning("옛 암호화 키 파일을 지우지 못했습니다")

    def reentry_required(self) -> list[str]:
        """다시 입력해야 하는 항목(설정 상태 응답에 낸다). 값은 담지 않는다."""
        with self._lock:
            return self._reentry_locked()

    def _reentry_locked(self) -> list[str]:
        cfg = self._read()
        reentry = list(cfg.get("reentry_required") or [])
        before = list(reentry)
        lost = self._secrets.lost()  # 어느 인스턴스가 격리했든 표시 파일로 안다
        if lost:
            if (cfg.get("telegram") or {}).get("chat_id") and "telegram" not in reentry:
                reentry.append("telegram")
            for supplier, cred in (cfg.get("credentials") or {}).items():
                item = f"credentials:{supplier}"
                if isinstance(cred, dict) and cred.get("login_id") and item not in reentry:
                    reentry.append(item)
        if "api_key" in reentry and KeySlots(self._v2_root, self._prot).active_fp() is not None:
            reentry.remove("api_key")
        if reentry != before:
            cfg["reentry_required"] = reentry
            self._save_config(cfg)
        if lost:
            self._secrets.clear_lost()  # 재입력 요청을 저장한 뒤에만 표시를 지운다
        return reentry

    def _resolved(self, item: str) -> None:
        cfg = self._read()  # 호출자가 self._lock 을 잡고 있다
        reentry = list(cfg.get("reentry_required") or [])
        if item in reentry:
            reentry.remove(item)
            cfg["reentry_required"] = reentry
            self._save_config(cfg)

    # ── 도매 계정 (credentials) ──

    def get_credentials(self, supplier: str) -> dict:
        """특정 도매상 계정 조회.

        Returns:
            {"login_id": "...", "login_pw": "..."} 또는
            {"login_id": "", "login_pw": ""} (미설정)
        """
        creds = self._read().get("credentials", {}).get(supplier)
        if not creds:
            return {"login_id": "", "login_pw": ""}
        return {
            "login_id": creds.get("login_id", ""),
            "login_pw": self._secrets.get(f"cred:{supplier}"),
        }

    def set_credentials(self, supplier: str, login_id: str, login_pw: str) -> None:
        """도매상 계정 저장 (비밀번호는 DPAPI 보호 저장소)"""
        self._secrets.set(f"cred:{supplier}", login_pw)  # 읽을 수 없으면 OSError(덮어쓰지 않는다)
        with self._lock:
            cfg = self._read()
            cfg.setdefault("credentials", {})[supplier] = {"login_id": login_id}
            self._save_config(cfg)
            self._resolved(f"credentials:{supplier}")

    def get_all_credentials(self) -> list[dict]:
        """전체 도매 계정 목록 조회 (비밀번호 마스킹)."""
        result = []
        stored = self._read().get("credentials", {})
        for supplier in SUPPLIERS:
            creds = stored.get(supplier)
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
        """텔레그램 설정 조회. {"token": "...", "chat_id": "..."}"""
        tg = self._read().get("telegram", {})
        return {
            "token": self._secrets.get("telegram_token"),
            "chat_id": tg.get("chat_id", ""),
        }

    def set_telegram(self, token: str, chat_id: str) -> None:
        """텔레그램 설정 저장 (토큰은 DPAPI 보호 저장소)"""
        self._secrets.set("telegram_token", token)
        with self._lock:
            cfg = self._read()
            cfg["telegram"] = {"chat_id": chat_id}
            self._save_config(cfg)
            self._resolved("telegram")

    # ── API 키 ──

    def get_api_key(self) -> str | None:
        """활성 API 키(v2 키 슬롯). 키는 설정의 검증 경로로만 바뀐다."""
        active = KeySlots(self._v2_root, self._prot).active()
        return active[1] if active else None

    # ── 포트 ──

    def get_port(self) -> int:
        """웹서버 포트 조회"""
        return self._read().get("port", 5900)

    def set_port(self, port: int) -> None:
        """웹서버 포트 저장"""
        with self._lock:
            cfg = self._read()
            cfg["port"] = port
            self._save_config(cfg)
