"""CLI 진입점: 모드 분기"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="domae_mcp",
        description="의약품 도매 통합 검색/주문 MCP 서버",
    )
    parser.add_argument("--mcp", action="store_true", help="MCP stdio 모드")
    parser.add_argument("--port", type=int, default=5900, help="웹서버 포트 (기본: 5900)")
    parser.add_argument("--install-startup", action="store_true", help="Windows 시작프로그램 등록")
    parser.add_argument("--uninstall-startup", action="store_true", help="Windows 시작프로그램 해제")
    parser.add_argument("--version", action="store_true", help="버전 출력")

    args = parser.parse_args()

    if args.version:
        from domae_mcp import __version__
        print(f"maipharm-domae-mcp v{__version__}")
        return

    if args.install_startup:
        from domae_mcp.local.startup import install_startup
        install_startup()
        return

    if args.uninstall_startup:
        from domae_mcp.local.startup import uninstall_startup
        uninstall_startup()
        return

    if args.mcp:
        from domae_mcp.local.mcp_server import run_mcp_server
        run_mcp_server()
    else:
        from domae_mcp.local.server import run_web_server
        run_web_server(port=args.port)


if __name__ == "__main__":
    main()
