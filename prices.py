# -*- coding: utf-8 -*-
"""일봉 종가 — 네이버 fchart (키 불필요, 종목·ETF·KOSPI 지수 공통)."""
import re
import time

import requests

_CACHE = {}


def daily_closes(symbol, count=400):
    """{'YYYYMMDD': close} 반환. symbol: '005930' 또는 'KOSPI'."""
    if symbol in _CACHE:
        return _CACHE[symbol]
    url = "https://fchart.stock.naver.com/sise.nhn"
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params={"symbol": symbol, "timeframe": "day", "count": count,
                                          "requestType": 0}, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            rows = re.findall(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', r.text)
            if not rows:
                raise ValueError("빈 응답")
            _CACHE[symbol] = {d: float(c) for d, c in rows}
            return _CACHE[symbol]
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"시세 실패 {symbol}: {last_err}")
