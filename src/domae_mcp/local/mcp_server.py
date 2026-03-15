"""MCP stdio 서버: 12개 도구를 제공하는 MCP 서버"""

import json
import logging
from datetime import datetime

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from domae_mcp.core.crawlers import CrawlerRegistry
from domae_mcp.core.models import (
    Order,
    Product,
    UrgentOrder,
    UrgentOrderSupplier,
    UrgentOrderLog,
)
from domae_mcp.core.services.search_service import SearchService
from domae_mcp.core.services.order_service import OrderService
from domae_mcp.core.services.monitor_service import MonitorService
from domae_mcp.core.services.telegram_service import TelegramService
from domae_mcp.local.config import ConfigManager, SUPPLIERS
from domae_mcp.local.database import init_db, _get_session_factory

logger = logging.getLogger(__name__)

server = Server("domae-mcp")

# ── 글로벌 상태 (서버 시작 시 초기화) ──

_config: ConfigManager | None = None
_session_factory = None
_search_service: SearchService | None = None
_order_service: OrderService | None = None
_monitor_service: MonitorService | None = None


def _init_services():
    """서비스 초기화"""
    global _config, _session_factory, _search_service, _order_service, _monitor_service

    _config = ConfigManager()
    init_db(_config)
    _session_factory = _get_session_factory(_config)

    # 크롤러 동적 로드 (MCP 모드 — 동기)
    api_key = _config.get_api_key()
    if api_key:
        from domae_mcp.core.crawlers.loader import CrawlerLoader
        loader = CrawlerLoader(_config.base_dir, api_key)
        try:
            crawlers = loader.load()
            CrawlerRegistry.register_all(crawlers)
        except RuntimeError as e:
            logger.warning("크롤러 로드 실패: %s", e)
    _search_service = SearchService()
    _order_service = OrderService()

    # 텔레그램 서비스
    tg = _config.get_telegram()
    telegram = TelegramService(token=tg["token"], chat_id=tg["chat_id"])

    # 모니터 서비스
    credentials = _get_all_credentials_dict()
    _monitor_service = MonitorService(
        db_session_factory=_session_factory,
        credentials=credentials,
        telegram_service=telegram,
    )


def _get_all_credentials_dict() -> dict[str, dict[str, str]]:
    """ConfigManager에서 전체 계정을 {도매상명: {login_id, login_pw}} 형태로 반환."""
    result = {}
    for supplier in SUPPLIERS:
        cred = _config.get_credentials(supplier)
        if cred.get("login_id") and cred.get("login_pw"):
            result[supplier] = cred
    return result


def _json_response(data) -> list[TextContent]:
    """결과를 TextContent로 래핑."""
    return [TextContent(type="text", text=json.dumps(data, ensure_ascii=False, default=str))]


# ── 도구 목록 정의 ──

TOOLS = [
    Tool(
        name="search_inventory",
        description="의약품 키워드로 10개 도매상(지오영, 복산, 인천, 티제이팜, HMP, 백제, 피코, 새로팜, 신덕팜, 대전동원약품)의 재고를 통합 검색합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "검색 키워드 (제품명 또는 보험코드)",
                },
                "suppliers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "검색할 도매상 목록 (미지정 시 전체)",
                },
            },
            "required": ["keyword"],
        },
    ),
    Tool(
        name="place_order",
        description="특정 도매상에 의약품을 주문합니다. product_id는 search_inventory 결과에서 얻을 수 있습니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "supplier": {"type": "string", "description": "도매상명"},
                "product_id": {"type": "string", "description": "도매상 내부 제품코드"},
                "product_name": {"type": "string", "description": "제품명"},
                "quantity": {"type": "integer", "description": "주문 수량"},
            },
            "required": ["supplier", "product_id", "product_name", "quantity"],
        },
    ),
    Tool(
        name="create_urgent_order",
        description="긴급주문을 등록합니다. 모니터링 중 해당 제품의 재고가 감지되면 자동으로 주문합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "product_name": {"type": "string"},
                "unit": {"type": "string"},
                "insurance_code": {"type": "string", "description": "보험코드"},
                "total_quantity": {"type": "integer", "description": "총 필요 수량"},
                "suppliers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "supplier": {"type": "string"},
                            "product_id": {"type": "string"},
                            "price": {"type": "integer"},
                        },
                    },
                    "description": "주문 가능한 도매상 목록 (search_inventory 결과에서 선택)",
                },
            },
            "required": ["product_name", "unit", "total_quantity", "suppliers"],
        },
    ),
    Tool(
        name="list_urgent_orders",
        description="긴급주문 목록과 진행 상태를 조회합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean", "default": False},
            },
        },
    ),
    Tool(
        name="cancel_urgent_order",
        description="긴급주문을 취소합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "urgent_order_id": {"type": "integer"},
            },
            "required": ["urgent_order_id"],
        },
    ),
    Tool(
        name="get_order_history",
        description="주문 이력을 조회합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    Tool(
        name="start_monitoring",
        description="재고 모니터링을 시작합니다. 등록된 제품들의 재고를 주기적으로 검색하고, 변동 시 텔레그램으로 알립니다.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="stop_monitoring",
        description="재고 모니터링을 중지합니다.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_monitoring_status",
        description="모니터링 실행 상태와 등록된 감시 제품 목록을 조회합니다.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="add_monitoring_product",
        description="모니터링 대상 제품을 추가합니다. 보험코드 또는 제품명으로 등록하면 주기적으로 재고를 검색합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "보험코드 또는 제품명"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="remove_monitoring_product",
        description="모니터링 대상 제품을 삭제합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "product_id": {"type": "integer", "description": "제품 ID"},
            },
            "required": ["product_id"],
        },
    ),
    Tool(
        name="test_credential",
        description="특정 도매상의 계정이 올바른지 실제 로그인을 시도하여 확인합니다.",
        inputSchema={
            "type": "object",
            "properties": {
                "supplier": {
                    "type": "string",
                    "description": "도매상명 (지오영, 복산, 인천, 티제이팜, HMP, 백제, 피코, 새로팜, 신덕팜, 대전동원약품)",
                },
            },
            "required": ["supplier"],
        },
    ),
]


@server.list_tools()
async def list_tools():
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    try:
        if name == "search_inventory":
            return await _handle_search_inventory(arguments)
        elif name == "place_order":
            return await _handle_place_order(arguments)
        elif name == "create_urgent_order":
            return await _handle_create_urgent_order(arguments)
        elif name == "list_urgent_orders":
            return await _handle_list_urgent_orders(arguments)
        elif name == "cancel_urgent_order":
            return await _handle_cancel_urgent_order(arguments)
        elif name == "get_order_history":
            return await _handle_get_order_history(arguments)
        elif name == "start_monitoring":
            return await _handle_start_monitoring(arguments)
        elif name == "stop_monitoring":
            return await _handle_stop_monitoring(arguments)
        elif name == "get_monitoring_status":
            return await _handle_get_monitoring_status(arguments)
        elif name == "add_monitoring_product":
            return await _handle_add_monitoring_product(arguments)
        elif name == "remove_monitoring_product":
            return await _handle_remove_monitoring_product(arguments)
        elif name == "test_credential":
            return await _handle_test_credential(arguments)
        else:
            return _json_response({"error": f"알 수 없는 도구: {name}"})
    except Exception as e:
        logger.error("도구 실행 에러: %s", name, exc_info=True)
        return _json_response({"error": str(e)})


# ── 도구 핸들러 ──


async def _handle_search_inventory(args: dict):
    keyword = args["keyword"]
    suppliers = args.get("suppliers")
    credentials = _get_all_credentials_dict()

    results = _search_service.search(
        keyword=keyword,
        suppliers=suppliers,
        credentials=credentials,
    )
    return _json_response({"keyword": keyword, "results": results})


async def _handle_place_order(args: dict):
    supplier = args["supplier"]
    product_id = args["product_id"]
    product_name = args["product_name"]
    quantity = args["quantity"]

    cred = _config.get_credentials(supplier)
    if not cred.get("login_id") or not cred.get("login_pw"):
        return _json_response({"success": False, "message": f"{supplier} 계정이 설정되지 않았습니다."})

    session = _session_factory()
    try:
        result = _order_service.place_order(
            supplier=supplier,
            product_id=product_id,
            product_name=product_name,
            quantity=quantity,
            credentials=cred,
            db_session=session,
        )
        return _json_response({"success": result.success, "message": result.message})
    finally:
        session.close()


async def _handle_create_urgent_order(args: dict):
    session = _session_factory()
    try:
        uo = UrgentOrder(
            product_name=args["product_name"],
            unit=args.get("unit", ""),
            insurance_code=args.get("insurance_code"),
            total_quantity=args["total_quantity"],
            filled_quantity=0,
            active=True,
            created_at=datetime.now(),
        )
        session.add(uo)
        session.flush()

        for sup_data in args.get("suppliers", []):
            uo_sup = UrgentOrderSupplier(
                urgent_order_id=uo.id,
                supplier=sup_data["supplier"],
                product_id=sup_data["product_id"],
                price=sup_data.get("price"),
            )
            session.add(uo_sup)

        session.commit()
        return _json_response({
            "success": True,
            "message": "긴급주문이 등록되었습니다.",
            "urgent_order_id": uo.id,
        })
    except Exception as e:
        session.rollback()
        raise
    finally:
        session.close()


async def _handle_list_urgent_orders(args: dict):
    active_only = args.get("active_only", False)
    session = _session_factory()
    try:
        query = session.query(UrgentOrder)
        if active_only:
            query = query.filter(UrgentOrder.active.is_(True))
        orders = query.order_by(UrgentOrder.created_at.desc()).all()

        result = []
        for uo in orders:
            result.append({
                "id": uo.id,
                "product_name": uo.product_name,
                "unit": uo.unit,
                "insurance_code": uo.insurance_code,
                "total_quantity": uo.total_quantity,
                "filled_quantity": uo.filled_quantity,
                "active": uo.active,
                "created_at": uo.created_at,
                "completed_at": uo.completed_at,
                "suppliers": [
                    {
                        "supplier": s.supplier,
                        "product_id": s.product_id,
                        "price": s.price,
                    }
                    for s in uo.suppliers
                ],
                "logs": [
                    {
                        "supplier": log.supplier,
                        "ordered_quantity": log.ordered_quantity,
                        "success": log.success,
                        "message": log.message,
                        "ordered_at": log.ordered_at,
                    }
                    for log in uo.logs
                ],
            })
        return _json_response({"orders": result})
    finally:
        session.close()


async def _handle_cancel_urgent_order(args: dict):
    urgent_order_id = args["urgent_order_id"]
    session = _session_factory()
    try:
        uo = session.query(UrgentOrder).filter(UrgentOrder.id == urgent_order_id).first()
        if not uo:
            return _json_response({"success": False, "message": "긴급주문을 찾을 수 없습니다."})
        if not uo.active:
            return _json_response({"success": False, "message": "이미 비활성화된 긴급주문입니다."})

        uo.active = False
        uo.completed_at = datetime.now()
        session.commit()
        return _json_response({"success": True, "message": "긴급주문이 취소되었습니다."})
    finally:
        session.close()


async def _handle_get_order_history(args: dict):
    limit = args.get("limit", 20)
    offset = args.get("offset", 0)
    session = _session_factory()
    try:
        orders = (
            session.query(Order)
            .order_by(Order.ordered_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        result = [
            {
                "id": o.id,
                "supplier": o.supplier,
                "product_name": o.product_name,
                "unit": o.unit,
                "quantity": o.quantity,
                "price": o.price,
                "success": o.success,
                "message": o.message,
                "is_urgent": o.is_urgent,
                "ordered_at": o.ordered_at,
            }
            for o in orders
        ]
        return _json_response({"orders": result})
    finally:
        session.close()


async def _handle_start_monitoring(args: dict):
    # 계정/텔레그램 최신화
    credentials = _get_all_credentials_dict()
    _monitor_service.update_credentials(credentials)

    tg = _config.get_telegram()
    _monitor_service.update_telegram(
        TelegramService(token=tg["token"], chat_id=tg["chat_id"])
    )

    _monitor_service.start()
    return _json_response({"success": True, "message": "모니터링을 시작했습니다."})


async def _handle_stop_monitoring(args: dict):
    _monitor_service.stop()
    return _json_response({"success": True, "message": "모니터링을 중지했습니다."})


async def _handle_get_monitoring_status(args: dict):
    session = _session_factory()
    try:
        products = session.query(Product).all()
        product_list = [
            {"id": p.id, "name": p.name, "description": p.description}
            for p in products
        ]
        return _json_response({
            "running": _monitor_service.is_running,
            "last_run": _monitor_service.last_run,
            "products": product_list,
        })
    finally:
        session.close()


async def _handle_add_monitoring_product(args: dict):
    name = args["name"]
    session = _session_factory()
    try:
        product = Product(name=name, created_at=datetime.now())
        session.add(product)
        session.commit()
        return _json_response({
            "success": True,
            "message": f"모니터링 제품 추가: {name}",
            "product_id": product.id,
        })
    finally:
        session.close()


async def _handle_remove_monitoring_product(args: dict):
    product_id = args["product_id"]
    session = _session_factory()
    try:
        product = session.query(Product).filter(Product.id == product_id).first()
        if not product:
            return _json_response({"success": False, "message": "제품을 찾을 수 없습니다."})

        name = product.name
        session.delete(product)
        session.commit()
        return _json_response({"success": True, "message": f"모니터링 제품 삭제: {name}"})
    finally:
        session.close()


async def _handle_test_credential(args: dict):
    supplier = args["supplier"]
    cred = _config.get_credentials(supplier)
    if not cred.get("login_id") or not cred.get("login_pw"):
        return _json_response({"success": False, "message": f"{supplier} 계정이 설정되지 않았습니다."})

    try:
        crawler = CrawlerRegistry.get(supplier)
        crawler.ensure_login(cred["login_id"], cred["login_pw"])
        return _json_response({"success": True, "message": "로그인 성공"})
    except Exception as e:
        return _json_response({"success": False, "message": f"로그인 실패: {e}"})


# ── 진입점 ──


async def _main():
    """MCP stdio 서버 실행."""
    _init_services()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def run_mcp_server():
    """CLI에서 호출하는 MCP 서버 진입점."""
    import asyncio

    asyncio.run(_main())
