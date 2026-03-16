"""도매 클라우드 워커 진입점"""
import sys
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

def main():
    from domae_mcp.cloud.worker import CloudWorker
    worker = CloudWorker()
    worker.run()

if __name__ == "__main__":
    main()
