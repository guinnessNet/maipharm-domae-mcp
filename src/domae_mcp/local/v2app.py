"""로컬 앱의 도매 v2 게이트 조립 (B-4 Task 8). 서버·MCP·설정 화면이 같은 게이트 하나를 쓴다."""
import logging
import threading

from domae_mcp.core.crawlers.registry import CrawlerBlocked
from domae_mcp.v2.gate import RuntimeGate
from domae_mcp.v2.keyslot import KeySlots
from domae_mcp.v2.loader import V2Error, V2Revoked
from domae_mcp.v2.runtime import build_kind
from domae_mcp.v2.store import StateStore, data_dir
from domae_mcp.v2.winprotect import default_protector

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_gate: RuntimeGate | None = None
MASKED_KEY = "dmk_..."  # setup-status 의 api_key_prefix

BLOCK_MESSAGES = {
    "key_revoked": "비활성화된 키입니다. 설정에서 새 키를 입력해 주세요.",
    "device_revoked": "이 기기는 키에서 해제되었습니다. 설정에서 키를 다시 확인해 주세요.",
    "protocol_too_old": "프로그램 업데이트가 필요합니다.",
    "clock_untrusted": "PC 시각이 맞지 않습니다. 시각을 확인하면 자동으로 다시 확인합니다.",
    "offline_deadline": "오프라인 사용 기한이 지났습니다. 인터넷 연결 뒤 자동으로 다시 받습니다.",
    "not_loaded": "크롤러가 아직 준비되지 않았습니다. 설정에서 API 키를 확인해 주세요.",
    "downgrade": "새 크롤러를 받는 중입니다. 잠시 뒤 다시 시도해 주세요.",
}

VERIFY_MESSAGES = {
    "auth_error": "유효하지 않은 키입니다.",
    "device_limit": "기기 한도(키당 2대)를 넘었습니다. 웹에서 기기를 정리한 뒤 다시 시도해 주세요.",
    "retry_later": "잠시 뒤 다시 시도해 주세요.",
    "stale": "잠시 뒤 다시 시도해 주세요.",
    "rejected": "잠시 뒤 다시 시도해 주세요.",
    "backoff": "잠시 뒤 다시 시도해 주세요.",
    "runtime_unsupported": "프로그램 업데이트가 필요합니다.",
    "protocol_too_old": "프로그램 업데이트가 필요합니다.",
    "rate_limited": "오늘 다운로드 한도에 도달했습니다. 내일 다시 시도해 주세요.",
    "transient": "서버 연결에 실패했습니다. 인터넷 연결을 확인해 주세요.",
    "key_revoked": "비활성화된 키입니다.",
    "device_revoked": "이 기기는 키에서 해제되었습니다.",
    "payload": "서버 응답 검증에 실패했습니다. PC 시각이 맞는지 확인한 뒤 다시 시도해 주세요.",
    "cancelled": "잠시 뒤 다시 시도해 주세요.",
}


def blocked_message(reason: str) -> str:
    return BLOCK_MESSAGES.get(reason, "크롤러를 사용할 수 없습니다.")


def blocked_body(e: CrawlerBlocked) -> dict:
    return {"error": "crawler_blocked", "reason": e.reason, "message": blocked_message(e.reason)}


def _build() -> RuntimeGate:
    prot = default_protector(build_kind())
    if not prot.protected:
        logger.warning("v2 로컬 저장소가 보호되지 않습니다(개발용 source 실행)")
    root = data_dir()
    gate = RuntimeGate(KeySlots(root, prot), StateStore(root, prot), prot)
    gate.install_guard()
    return gate


def get_gate() -> RuntimeGate:
    global _gate
    with _lock:
        if _gate is None:
            _gate = _build()
        return _gate


def set_gate(gate: RuntimeGate | None) -> None:
    """시험용 주입."""
    global _gate
    with _lock:
        _gate = gate
    if gate is not None:
        gate.install_guard()


def start_background() -> threading.Thread:
    gate = get_gate()

    def run():
        try:
            gate.start()
        except Exception as e:
            logger.warning("v2 시작 흐름 실패: %s", type(e).__name__)

    t = threading.Thread(target=run, name="domae-v2-start", daemon=True)
    t.start()
    return t


def verify_key(api_key: str) -> tuple[bool, str, int]:
    """설정의 키 검증 = v2 초기화(P13). 실패하면 활성 키·캐시·레지스트리는 그대로다."""
    if api_key == MASKED_KEY:  # 설정 화면이 보여 준 가린 값 그대로 = 활성 키 재검증
        try:
            n = get_gate().reverify()
        except V2Revoked as e:
            return False, VERIFY_MESSAGES.get(e.reason, "비활성화된 키입니다."), 0
        except V2Error as e:
            if e.reason == "no_key":
                return False, "API 키를 입력해 주세요.", 0
            return False, VERIFY_MESSAGES.get(e.reason, "서버 응답 검증에 실패했습니다. 잠시 뒤 다시 시도해 주세요."), 0
        except Exception as e:
            logger.warning("v2 재검증 중 오류: %s", type(e).__name__)
            return False, "잠시 뒤 다시 시도해 주세요.", 0
        return True, f"인증 완료! 크롤러 {n}개 로드됨.", n
    if not isinstance(api_key, str) or not api_key.startswith("dmk_") or len(api_key) > 200 or not api_key.isascii():
        return False, "API 키 형식이 올바르지 않습니다. (dmk_ 로 시작해야 합니다)", 0
    try:
        n = get_gate().switch(api_key)
    except V2Revoked as e:
        return False, VERIFY_MESSAGES.get(e.reason, "비활성화된 키입니다."), 0
    except V2Error as e:
        logger.warning("v2 키 검증 실패: %s", e.reason)
        if e.reason in ("load",) and e.detail == "client_too_old":
            return False, VERIFY_MESSAGES["runtime_unsupported"], 0
        return False, VERIFY_MESSAGES.get(e.reason, "서버 응답 검증에 실패했습니다. 잠시 뒤 다시 시도해 주세요."), 0
    except Exception as e:
        logger.warning("v2 키 검증 중 오류: %s", type(e).__name__)
        return False, "키를 저장하지 못했습니다. 잠시 뒤 다시 시도해 주세요.", 0
    return True, f"인증 완료! 크롤러 {n}개 로드됨.", n
