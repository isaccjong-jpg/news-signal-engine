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


_SPX = {}


def spx_closes():
    """S&P500 일봉 {'YYYYMMDD'(미국 현지일): close}. 네이버 해외지수 API → 실패 시 Yahoo 차트 API(둘 다 키 불필요)."""
    if _SPX:
        return _SPX
    errs = []
    try:
        r = requests.get("https://api.stock.naver.com/chart/foreign/index/.INX",
                         params={"periodType": "dayCandle"}, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        rows = {p["localDate"]: float(p["closePrice"]) for p in r.json().get("priceInfos", [])}
        if len(rows) > 30:
            _SPX.update(rows)
            return _SPX
        errs.append("naver 빈 응답")
    except Exception as e:
        errs.append(f"naver {e}")
    try:
        from datetime import datetime, timezone, timedelta
        r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
                         params={"range": "1y", "interval": "1d"}, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        ny = timezone(timedelta(hours=-4))  # 날짜만 필요(장 마감 16시 기준 -4/-5 어느 쪽이든 같은 날)
        for ts, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
            if c is not None:
                _SPX[datetime.fromtimestamp(ts, ny).strftime("%Y%m%d")] = float(c)
        if len(_SPX) > 30:
            return _SPX
        errs.append("yahoo 빈 응답")
    except Exception as e:
        errs.append(f"yahoo {e}")
    raise RuntimeError("S&P500 시세 실패: " + " / ".join(errs))


def latest(symbol):
    """신호 시점 스냅샷: (가격, 일자). 장중이면 네이버 당일 봉의 현재가, 장외면 직전 종가."""
    if symbol == "SPX":
        px = spx_closes()
    else:
        px = daily_closes(symbol)
    d = max(px)
    return px[d], d
