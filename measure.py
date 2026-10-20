# -*- coding: utf-8 -*-
"""1/5/20 영업일 후 수익률·KOSPI 대비 초과수익 측정 + 요약 통계.

미래정보 차단 규칙
- P0(판단시점가): 판단 시각 이전에 '이미 확정된' 마지막 종가.
- 기준가(진입가): 판단 후 실제로 체결 가능한 첫 종가.
  · 영업일 15:20 이전 판단 → 당일 종가(동시호가 진입 가정)
  · 그 외(15:20 이후·휴장일) → 다음 영업일 종가
- +Nd: 기준일로부터 N 영업일 뒤 종가.
- 벤치마크(미래정보 없음 — 기준일 이전 데이터로만 추정):
  · 주식: β × KOSPI%. β는 기준일 직전 120영업일 일간수익률 회귀(0~2.5로 클립). 베타 1 가정 제거.
  · 자산ETF(금·원유·달러·채권·인버스): 그 자산의 직전 120영업일 평균 N일 드리프트. (KOSPI와 비교하면 KOSPI 역방향을 재고, 절대수익은 추세장에서 전부 적중으로 나오므로)
- 방향보정 초과 = (종목% - 벤치%) × (+1 / -1). 양수면 판단 적중.
- 요약 t값: 사건 단위 평균 → 기준일 ISO주 단위 클러스터 표준오차. 같은 주의 사건·겹치는 5일 구간은 독립이 아니므로.
"""
import math
from datetime import datetime, time as dtime

from config import KST
from prices import daily_closes, spx_closes

HORIZONS = (1, 5, 20)
CUTOFF = dtime(15, 20)
LOOKBACK = 120


def _beta(px, bench, days, i0):
    """기준일(i0) 직전 LOOKBACK 영업일 일간수익률로 β 추정. 데이터 부족 시 1.0."""
    xs, ys = [], []
    for k in range(max(1, i0 - LOOKBACK), i0):
        d1, d0 = days[k], days[k - 1]
        if d1 in px and d0 in px and d1 in bench and d0 in bench:
            xs.append(bench[d1] / bench[d0] - 1)
            ys.append(px[d1] / px[d0] - 1)
    if len(xs) < 40:
        return 1.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var == 0:
        return 1.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    return max(0.0, min(2.5, b))


def _drift(px, days, i0, h):
    """기준일 직전 LOOKBACK 구간에서 h영업일 수익률의 평균(%). 자산ETF 추세 제거용."""
    vals = []
    for k in range(max(0, i0 - LOOKBACK), i0 - h):
        a, b = days[k], days[k + h]
        if a in px and b in px:
            vals.append((px[b] / px[a] - 1) * 100)
    return sum(vals) / len(vals) if len(vals) >= 20 else 0.0


def _final_days(closes, now_kst):
    days = sorted(closes)
    today = now_kst.strftime("%Y%m%d")
    if days and days[-1] == today and now_kst.time() < dtime(16, 0):
        days = days[:-1]  # 장중/직후 미확정 봉 제외
    return days


def _base_dates(signal_kst, bench, days):
    """(기준일, 판단시점 확정종가일). 기준일 = 판단 후 실제 체결 가능한 첫 종가일."""
    d = signal_kst.strftime("%Y%m%d")
    if d in bench and signal_kst.time() <= CUTOFF:
        return d, max((x for x in days if x < d), default=None)
    nxt = [x for x in sorted(bench) if x > d]
    return (nxt[0] if nxt else None), max((x for x in days if x <= d), default=None)


def _spx_before(spx, yyyymmdd):
    """KRX 일자 D 종가 시점에 이미 확정된 S&P500 종가 = 미국 현지일 < D 인 마지막 종가(미국장은 D 새벽에 마감)."""
    d = max((x for x in spx if x < yyyymmdd), default=None)
    return spx[d] if d else None


def measure_simple(signal_kst, code, now_kst):
    """단순 지수 대비 초과수익(사용자 지시): 초과 = 종목% − 지수%. 지수 = KOSPI, S&P500 두 가지.
    기준일·N영업일 규칙은 measure_one과 동일(KRX 영업일). 베타 조정 1차 지표와 별개의 참고 지표."""
    bench = daily_closes("KOSPI")
    days = _final_days(bench, now_kst)
    base_date, _ = _base_dates(signal_kst, bench, days)
    res = {"base_date": base_date, "base": None, "status": "대기"}
    for h in HORIZONS:
        for k in ("r", "k", "sp", "xk", "xs"):
            res[f"{k}{h}"] = None
    if base_date is None or base_date not in days:
        return res
    px = daily_closes(code)
    if base_date not in px:
        res["status"] = "결측"
        return res
    try:
        spx = spx_closes()
    except Exception:
        spx = {}
    base, kb = px[base_date], bench[base_date]
    sb = _spx_before(spx, base_date)
    res["base"] = base
    i0 = days.index(base_date)
    done = 0
    for h in HORIZONS:
        if i0 + h >= len(days) or days[i0 + h] not in px:
            continue
        dh = days[i0 + h]
        r = (px[dh] / base - 1) * 100
        k = (bench[dh] / kb - 1) * 100
        res[f"r{h}"], res[f"k{h}"], res[f"xk{h}"] = round(r, 3), round(k, 3), round(r - k, 3)
        se = _spx_before(spx, dh)
        if sb and se:
            sp = (se / sb - 1) * 100
            res[f"sp{h}"], res[f"xs{h}"] = round(sp, 3), round(r - sp, 3)
        done += 1
    res["status"] = "완료" if done == len(HORIZONS) else "진행"
    return res


def measure_one(signal_kst, code, direction, now_kst, absolute=False):
    """반환 dict (값 없는 항목은 None). status: 대기/진행/완료/결측. absolute=True → 벤치 0(자산ETF)."""
    bench = daily_closes("KOSPI")  # 영업일 캘린더 기준은 항상 KOSPI
    days = _final_days(bench, now_kst)
    base_date, p0_date = _base_dates(signal_kst, bench, days)
    res = {"base_date": base_date, "base": None, "pre": None, "beta": None, "status": "대기"}
    for h in HORIZONS:
        res[f"r{h}"] = res[f"b{h}"] = res[f"x{h}"] = res[f"s{h}"] = None
    if base_date is None or base_date not in days:
        return res

    px = daily_closes(code)
    if base_date not in px:
        res["status"] = "결측"
        return res
    base, bbase = px[base_date], bench[base_date]
    res["base"] = base
    if p0_date and p0_date in px:
        res["pre"] = round((base / px[p0_date] - 1) * 100, 3)
    i0 = days.index(base_date)
    sign = 1 if direction == "+" else -1
    beta = None if absolute else _beta(px, bench, days, i0)
    res["beta"] = round(beta, 2) if beta is not None else "drift"
    done = 0
    for h in HORIZONS:
        if i0 + h < len(days):
            dh = days[i0 + h]
            if dh not in px:
                continue
            r = (px[dh] / base - 1) * 100
            b = _drift(px, days, i0, h) if absolute else beta * (bench[dh] / bbase - 1) * 100
            res[f"r{h}"], res[f"b{h}"] = round(r, 3), round(b, 3)
            res[f"x{h}"] = round(r - b, 3)
            res[f"s{h}"] = round(sign * (r - b), 3)
            done += 1
    res["status"] = "완료" if done == len(HORIZONS) else "진행"
    return res


def _stats(vals):
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean": None, "hit": None, "t": None}
    m = sum(vals) / n
    hit = sum(1 for v in vals if v > 0) / n * 100
    if n > 1:
        sd = math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))
        t = m / (sd / math.sqrt(n)) if sd > 0 else None
    else:
        t = None
    return {"n": n, "mean": round(m, 3), "hit": round(hit, 1), "t": round(t, 2) if t is not None else None}


def _cluster_stats(ev_vals, clusters):
    """사건값 리스트 + 각 사건의 클러스터(ISO주). 평균은 사건 평균, SE는 클러스터 합 기준(cluster-robust)."""
    base = _stats(ev_vals)
    n = len(ev_vals)
    if n == 0:
        return base
    m = base["mean"]
    sums = {}
    for v, c in zip(ev_vals, clusters):
        sums[c] = sums.get(c, 0.0) + (v - m)
    g = len(sums)
    base["clusters"] = g
    if g > 1:
        var = sum(x ** 2 for x in sums.values()) * g / (g - 1)
        se = math.sqrt(var) / n
        base["t"] = round(m / se, 2) if se > 0 else None
    else:
        base["t"] = None
    return base


def summarize(rows):
    """rows: [{'signal_id','category','confidence','week','s1','s5','s20'}].
    1) 같은 사건의 여러 종목 → 평균 1개(사건 단위)  2) t값은 ISO주 클러스터 표준오차.
    'clusters' = 표본이 퍼져 있는 주 수(사전등록 조건: ≥8)."""
    groups = {"전체": rows}
    for r in rows:
        groups.setdefault(f"분류:{r['category']}", []).append(r)
        groups.setdefault(f"확신도:{r['confidence']}", []).append(r)
    out = []
    for g, rs in groups.items():
        line = {"group": g}
        for h in HORIZONS:
            by_ev, wk = {}, {}
            for r in rs:
                v = r.get(f"s{h}")
                if v is not None:
                    by_ev.setdefault(r["signal_id"], []).append(v)
                    wk[r["signal_id"]] = r.get("week", "")
            ids = list(by_ev)
            ev_vals = [sum(by_ev[i]) / len(by_ev[i]) for i in ids]
            line[h] = _cluster_stats(ev_vals, [wk[i] for i in ids])
        out.append(line)
    out.sort(key=lambda x: (x["group"] != "전체", x["group"]))
    return out


def iso_week(yyyymmdd):
    try:
        y, w, _ = datetime.strptime(str(yyyymmdd), "%Y%m%d").isocalendar()
        return f"{y}-W{w:02d}"
    except Exception:
        return ""


def parse_kst(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
