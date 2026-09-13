# -*- coding: utf-8 -*-
"""
stock_monitor.py 핫픽스 — 네이버 PC 금융 시세표 폐지 대응 (2026-09-11)

증상: 봇이 매 스캔 "데이터 없음 — 재시도"만 반복하고 data.json 이 갱신되지 않음.
원인: finance.naver.com/sise/sise_quant.naver 등 구 PC 시세 페이지가
      stock.naver.com SPA 로 리다이렉트 → HTML 에 표가 없어 파서가 0건 반환.
조치: 시세 수집을 m.stock.naver.com JSON API 로 교체.

사용법:  python3 patch_naver_api.py          (stock_monitor.py 를 제자리 수정)
        원본은 stock_monitor.py.bak-naverapi 로 백업됨. 되돌리려면 그 파일을 복사.
"""
import os, re, shutil, sys, py_compile

TARGET = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stock_monitor.py")
START = "def fetch_market_data() -> dict:"
END = "def _parse_quant_page(url: str, market: str) -> list:"

NEW = '''# ─── 네이버 모바일 시세 API (2026-09-11: PC 금융 시세표 폐지 대응) ───
#   구 PC 페이지(finance.naver.com/sise/sise_quant.naver 등)가 stock.naver.com SPA 로
#   리다이렉트되면서 HTML 표가 사라짐 → 요청은 200 이지만 파싱 0건 → "데이터 없음" 무한 재시도.
#   대체 = m.stock.naver.com/api/stocks/{정렬}/{시장} JSON.
#   살아있는 정렬: up(상승률) / down(하락률) / marketValue(시총). 거래대금 정렬은 없음
#   → up + marketValue 합집합을 거래대금순으로 세워 trans 를 구성한다.
NAVER_API_HEADERS = {
    "User-Agent": NAVER_HEADERS["User-Agent"],
    "Referer": "https://m.stock.naver.com/",
    "Accept": "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

# 네이버 등락 구분 코드: 1=상한 2=상승 3=보합 4=하한 5=하락
_DOWN_CODES = ("4", "5")


def _api_num(*vals) -> float:
    """'2,390' / 2390 / None 혼재 → float (첫 유효값)."""
    for v in vals:
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return float(v)
        t = re.sub(r'[^0-9.\\-]', '', str(v))
        if t not in ('', '-', '.', '-.'):
            try:
                return float(t)
            except ValueError:
                pass
    return 0.0


def _api_stock_list(sort: str, market: str, page: int = 1, size: int = 100) -> list:
    """네이버 모바일 시세 API → 기존 파서와 동일한 형태의 dict 리스트."""
    url = (f"https://m.stock.naver.com/api/stocks/{sort}/{market}"
           f"?page={page}&pageSize={size}")
    results = []
    try:
        res = requests.get(url, headers=NAVER_API_HEADERS, timeout=10)
        if res.status_code != 200:
            print(f"  [네이버API/{market}/{sort}] HTTP {res.status_code}")
            return results
        for s in (res.json() or {}).get("stocks", []):
            if s.get("stockEndType") not in (None, "stock"):
                continue                       # ETF/ETN 등 제외
            code = str(s.get("itemCode") or "").strip()
            name = str(s.get("stockName") or "").strip()
            if not re.fullmatch(r"\\d{6}", code) or not name:
                continue
            rate = abs(_api_num(s.get("fluctuationsRatio")))
            if str((s.get("compareToPreviousPrice") or {}).get("code") or "") in _DOWN_CODES:
                rate = -rate                   # API 는 등락률을 절대값으로 준다
            amt = _api_num(s.get("accumulatedTradingValueRaw"))
            if not amt:                        # 폴백: 백만원 단위 필드
                amt = _api_num(s.get("accumulatedTradingValue")) * 1_000_000
            cap_raw = _api_num(s.get("marketValueRaw"))
            cap_eok = int(cap_raw / 100_000_000) if cap_raw else int(_api_num(s.get("marketValue")))
            results.append({
                "코드":     code,
                "종목명":   name,
                "시장":     market,
                "종가":     int(_api_num(s.get("closePriceRaw"), s.get("closePrice"))),
                "등락률":   round(rate, 2),
                "거래대금": int(amt),          # 원
                "시총억":   cap_eok,           # 억원
                "순위":     0,
            })
    except Exception as e:
        print(f"  [네이버API/{market}/{sort}] 오류: {e}")
    return results


def fetch_market_data() -> dict:
    """
    네이버 모바일 시세 API 에서 데이터 수집.
    반환: {
      "trans": [...],   # 거래대금 내림차순 (KOSPI+KOSDAQ)
      "rise":  [...],   # 등락률 내림차순 (상승 종목만)
      "all":   {code: stock}  # 코드별 통합 (상한가 탐지용)
    }
    """
    rise, cap = [], []
    for market in ("KOSPI", "KOSDAQ"):
        rise += _api_stock_list("up", market, 1, 100)           # 상승률 상위 100
        cap  += _api_stock_list("marketValue", market, 1, 100)  # 시총 상위 100(대금 참조용)

    # 코드별 통합 — 같은 종목이 두 목록에 있으면 거래대금이 큰(=더 최신) 쪽 유지
    allmap = {}
    for s in cap + rise:
        prev = allmap.get(s["코드"])
        if prev is None or s["거래대금"] > prev["거래대금"]:
            allmap[s["코드"]] = s

    return {
        "trans": sorted(allmap.values(), key=lambda x: x["거래대금"], reverse=True),
        "rise":  sorted([s for s in allmap.values() if s["등락률"] > 0],
                        key=lambda x: x["등락률"], reverse=True),
        "all":   allmap,
    }


'''


def main():
    if not os.path.exists(TARGET):
        sys.exit("stock_monitor.py 를 찾을 수 없습니다: %s" % TARGET)
    src = open(TARGET, encoding="utf-8").read()

    if "NAVER_API_HEADERS" in src:
        print("이미 패치된 파일입니다. 변경 없음.")
        return
    i, j = src.find(START), src.find(END)
    if i < 0 or j < 0 or j <= i:
        sys.exit("패치 지점을 찾지 못했습니다 (fetch_market_data / _parse_quant_page 앵커 불일치).")

    bak = TARGET + ".bak-naverapi"
    shutil.copy2(TARGET, bak)
    out = src[:i] + NEW + src[j:]
    open(TARGET, "w", encoding="utf-8").write(out)

    py_compile.compile(TARGET, doraise=True)
    print("패치 완료")
    print("  백업     :", bak)
    print("  교체 구간: %d → %d bytes" % (j - i, len(NEW)))
    print("  문법 검사: OK")


if __name__ == "__main__":
    main()
