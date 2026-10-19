"""exe 에 크롤러 런타임 import 대상을 명시적으로 포함한다(스펙 B3, P18).

marshal 크롤러는 실행 때 이 모듈들을 import 한다. 얼린 exe 는 소스에서 보이지 않는 import 를 따라가지 않으므로
빌드가 이 모듈을 포함(--include-module)해 의존 패키지를 함께 넣는다. 목록은 scripts/check_crawler_imports.py 가
seeds 의 실제 import 와 대조한다.
"""
import base64  # noqa: F401
import datetime  # noqa: F401
import importlib
import inspect  # noqa: F401
import json  # noqa: F401
import logging  # noqa: F401
import math  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import time  # noqa: F401
import typing  # noqa: F401
import urllib.parse  # noqa: F401

import bs4  # noqa: F401
import bs4.builder  # noqa: F401
import lxml  # noqa: F401
import lxml.etree  # noqa: F401
import lxml.html  # noqa: F401
import requests  # noqa: F401
import urllib3  # noqa: F401

# 외부 허용 목록(최상위 이름)
EXTERNAL_TOP = ("re", "json", "logging", "time", "math", "base64", "os", "inspect", "typing", "datetime", "urllib",
                "requests", "urllib3", "bs4", "lxml")
# 내부 ABI 허용 목록(전체 경로가 같아야 한다)
INTERNAL_ABI = ("domae_mcp.core.crawlers.base", "domae_mcp.core.crawlers.cart_snapshot")
_IMPORT_CHECK = ("re", "json", "logging", "time", "math", "base64", "os", "inspect", "typing", "datetime",
                 "urllib.parse", "requests", "urllib3", "bs4", "lxml", "lxml.etree", "lxml.html") + INTERNAL_ABI


def check() -> bool:
    """크롤러가 쓰는 모듈이 모두 import 되고 bs4 가 lxml 파서를 쓸 수 있는지 본다(--v2-selftest-out)."""
    try:
        for name in _IMPORT_CHECK:
            importlib.import_module(name)
        bs4.BeautifulSoup("<p>x</p>", "lxml")
        bs4.BeautifulSoup("<p>x</p>", "html.parser")
    except Exception:
        return False
    return True
