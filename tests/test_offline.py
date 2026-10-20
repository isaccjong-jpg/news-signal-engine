# -*- coding: utf-8 -*-
"""오프라인 점검: 측정 로직 수기 대조 + 시트 대역(FakeSheet)으로 collect→measure 전 경로 실행."""
import json, sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main, sheets
from config import KST, load_config
from measure import measure_one, parse_kst
from prices import daily_closes

now = datetime.now(KST)
# 1) 장중 신호 → 당일 종가 기준
m = measure_one(parse_kst("2026-09-01 10:00"), "010950", "+", now)
px, kp = daily_closes("010950"), daily_closes("KOSPI")
d = sorted(kp); i = d.index("20260901")
exp5 = (px[d[i+5]]/px["20260901"]-1)*100 - m["beta"]*(kp[d[i+5]]/kp["20260901"]-1)*100
assert m["base_date"] == "20260901" and abs(m["x5"] - round(exp5, 3)) < 2e-3, (m, exp5)
print("장중 OK", m["base_date"], m["s1"], m["s5"], m["s20"], m["status"])
# 2) 15:20 이후 → 다음 영업일
m = measure_one(parse_kst("2026-09-01 15:25"), "010950", "-", now)
assert m["base_date"] == d[i+1] and m["s5"] == -m["x5"], m; print("장후 OK", m["base_date"])
# 3) 주말 신호 → 월요일
m = measure_one(parse_kst("2026-09-05 11:00"), "005930", "+", now); print("주말 →", m["base_date"])
# 4) 오늘 장마감 전 미확정 → 대기
m = measure_one(parse_kst("2026-10-15 20:00"), "005930", "+", now); print("미확정 →", m["status"], m["base_date"])

class FakeWS:
    def __init__(s, h): s.rows=[h]
class FakeSheet(sheets.Sheet):
    def __init__(s):
        s.t = {k: [list(v)] for k, v in sheets.TABS.items()}
    def append(s, tab, rows): s.t[tab] += [list(r) for r in rows]
    def append_archive(s, month, rows): s.archive = getattr(s, "archive", [["h"]]) + [list(r) for r in rows]
    def records(s, tab):
        h = s.t[tab][0]; return [dict(zip(h, r + [""]*(len(h)-len(r)))) for r in s.t[tab][1:]]
    def update_rows(s, tab, ups):
        col = lambda c: sum((ord(ch)-64)*26**k for k, ch in enumerate(reversed(c))) - 1
        for ri, c, vals in ups:
            row = s.t[tab][ri-1]; st = col(c)
            row += [""]*(st+len(vals)-len(row)); row[st:st+len(vals)] = vals
    def replace_body(s, tab, rows): s.t[tab] = [s.t[tab][0]] + [list(r) for r in rows]
    def get_state(s, k, default=None):
        for r in s.t["_state"][1:]:
            if r[0] == k: return json.loads(r[1])
        return default
    def set_state(s, k, v):
        s.t["_state"] = [s.t["_state"][0]] + [r for r in s.t["_state"][1:] if r[0] != k] + [[k, json.dumps(v)]]

cfg = load_config(); fs = FakeSheet()
main.run_collect(cfg, fs, str(Path(__file__).parent / "mock_llm.json"))
# 과거 시각 신호를 하나 주입해 측정 완료 경로까지 확인
r = list(fs.t["측정"][1]); r[0] = "SX-0"; r[1] = "SX"; r[2] = "2026-08-20 10:00"; fs.t["측정"].append(r)
assert all(len(x) == 38 for x in fs.t["측정"]), f"측정 열수 불일치 {[len(x) for x in fs.t['측정']]}"
assert all(len(x) == 32 for x in fs.t["바구니"]), f"바구니 열수 불일치 {[len(x) for x in fs.t['바구니']]}"
assert len(fs.t["바구니"]) > 1, "섹터 바구니 행 없음"
hm = fs.t["측정"][0]; d0 = dict(zip(hm, fs.t["측정"][1]))
assert d0["신호시점가"] != "" and d0["KOSPI@신호"] != "" and d0["SPX@신호"] != "", f"신호시점 스냅샷 비어있음 {d0['신호시점가']}/{d0['KOSPI@신호']}/{d0['SPX@신호']}"
print("신호시점 스냅샷 OK", d0["종목명"], d0["신호시점가"], "KOSPI", d0["KOSPI@신호"], "SPX", d0["SPX@신호"])
# 바구니에도 과거 시각 행 주입 → 측정 완료 경로
rb = list(fs.t["바구니"][1]); rb[0] = "SX-B00"; rb[1] = "SX"; rb[2] = "2026-08-20 10:00"; fs.t["바구니"].append(rb)
assert all(len(x) == 12 for x in fs.t["신호로그"]), "신호로그 열수 불일치"
assert len(fs.archive) > 1, "뉴스원문 미보관"
main.run_measure(cfg, fs)
h = fs.t["측정"][0]
for row in fs.t["측정"][1:]:
    d = dict(zip(h, row)); print(d["측정ID"], d["종목명"], d["방향"], d["기준일"], d["방향보정1d"], d["방향보정5d"], d["방향보정20d"], d["상태"],
                               "| 단순K5d", d["단순초과K5d"], "단순S5d", d["단순초과S5d"])
hb = fs.t["바구니"][0]
for row in fs.t["바구니"][1:]:
    d = dict(zip(hb, row)); print("  바구니", d["바구니ID"], d["섹터"], d["구분"], d["종목명"], d["방향"], d["기준일"], d["초과K5d"], d["초과S5d"], d["상태"])
dx = dict(zip(h, fs.t["측정"][-1]))
if dx["상태"] == "완료":
    # 단순 초과 수기 대조: 종목5d% − KOSPI5d%  (베타 조정 열의 5d%·벤치는 β×KOSPI이므로 직접 재계산)
    from measure import measure_simple
    ms = measure_simple(parse_kst(dx["판단시각(KST)"]), str(dx["종목코드"]).zfill(6), now)
    assert abs(ms["xk5"] - float(dx["단순초과K5d"])) < 1e-6 and abs(ms["r5"] - float(dx["5d%"])) < 1e-6, (ms, dx)
    print("단순초과 수기대조 OK", ms["r5"], "-", ms["k5"], "=", ms["xk5"], "| SPX5d", ms["sp5"], "→", ms["xs5"])
    bx = dict(zip(hb, fs.t["바구니"][-1]))
    assert bx["상태"] == "완료" and bx["기준일"] == dx["기준일"], bx
for row in fs.t["요약"][1:5]: print("요약:", row[:2], row[6:10], row[14])
assert fs.t["요약"][1][14] != "", "주클러스터수 미기록"
# 재실행 시 seen 해시로 중복 헤드라인 제외되는지
n_before = len(fs.get_state("seen_news", {}))
print("seen 해시", n_before, "| 수익률스냅샷", len(fs.get_state("recent_returns")["data"]))
# 자산ETF 절대수익 검증
m = measure_one(parse_kst("2026-09-01 10:00"), "132030", "+", now, absolute=True)
assert m["beta"] == "drift" and m["b5"] != 0.0, m; print("자산ETF 드리프트 벤치 OK", m["r5"], m["b5"])
m = measure_one(parse_kst("2026-09-01 10:00"), "000660", "+", now)
assert isinstance(m["beta"], float) and m["beta"] > 1.0, m; print("베타 조정 OK", m["beta"])
import classifier
u = classifier._build_user([{"id":"n0","ts_kst":"x","source":"s","title":"t"}], [], main.UNIVERSE, {"005930": (1.2, -3.4)})
assert "(5d +1.2%/20d -3.4%)" in u; print("수익률 프롬프트 주입 OK")
