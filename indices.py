"""
주요 지수/환율 수집 (뉴스 탭 상단 카드용).
- 코스피 / 코스닥 : 지수값 + 등락 + 투자자별 순매수(외국인/개인/기관)
- 나스닥 / S&P500 : 지수값 + 등락
- 원/달러 환율      : 값 + 등락

네이버 금융 모바일 API(api.stock.naver.com)를 사용.
전부 fail-soft: 어떤 지수 하나가 실패해도 그 카드만 빠지고 나머지는 정상.

서버에서 1회 검증:
    python3 indices.py            # 수집 결과(JSON) 출력 → 값이 맞는지 확인
"""
import json
import re
import requests

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
    "Accept": "application/json",
    "Referer": "https://m.stock.naver.com/",
}
_TIMEOUT = 8


def _get_json(url: str):
    r = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _f(v):
    """'2,647.46' / '1,380.5' / 숫자 → float. 실패 시 None."""
    if v is None:
        return None
    try:
        return float(re.sub(r"[^\d.\-]", "", str(v)))
    except (ValueError, TypeError):
        return None


def _dir(node) -> int:
    """compareToPreviousPrice 노드 → +1 상승 / -1 하락 / 0 보합."""
    if isinstance(node, dict):
        code = str(node.get("code", ""))
        name = str(node.get("name", "")) + str(node.get("text", ""))
        if code in ("2", "1") or "RISE" in name.upper() or "상승" in name:
            return 1
        if code in ("5", "4") or "FALL" in name.upper() or "하락" in name:
            return -1
    return 0


def _basic(symbol: str):
    """지수/환율 basic → (value, change, rate, direction). 실패 시 None."""
    j = _get_json(f"https://api.stock.naver.com/index/{symbol}/basic")
    value = _f(j.get("closePrice"))
    change = _f(j.get("compareToPreviousClosePrice"))
    rate = _f(j.get("fluctuationsRatio"))
    d = _dir(j.get("compareToPreviousPrice"))
    if d < 0 and change and change > 0:
        change = -change
    return {"value": value, "change": change, "rate": rate, "dir": d}


def _fx(symbol="FX_USDKRW"):
    j = _get_json(f"https://api.stock.naver.com/marketindex/exchange/{symbol}/basic")
    value = _f(j.get("closePrice"))
    change = _f(j.get("compareToPreviousClosePrice") or j.get("fluctuations"))
    rate = _f(j.get("fluctuationsRatio"))
    d = _dir(j.get("compareToPreviousPrice"))
    if d < 0 and change and change > 0:
        change = -change
    return {"value": value, "change": change, "rate": rate, "dir": d}


def _domestic_investors(code: str):
    """코스피/코스닥 투자자별 순매수(외국인/개인/기관). 실패 시 None.
    단위는 네이버 원자료(보통 백만원). 파싱 실패해도 지수 카드는 유지."""
    for url in (
        f"https://api.stock.naver.com/index/{code}/investorTrend",
        f"https://api.stock.naver.com/index/{code}/investors",
    ):
        try:
            j = _get_json(url)
            rows = j.get("investorTrends") or j.get("result") or j.get("datas") or j
            if isinstance(rows, dict):
                rows = rows.get("list") or rows.get("items") or [rows]
            row = rows[0] if isinstance(rows, list) and rows else rows
            if not isinstance(row, dict):
                continue
            def pick(*keys):
                for k in keys:
                    if k in row:
                        return _f(row[k])
                return None
            foreign = pick("foreignerPureBuyQuant", "foreign", "frgn", "foreignerNetBuy")
            indiv = pick("individualPureBuyQuant", "individual", "indi", "individualNetBuy")
            inst = pick("organizationPureBuyQuant", "organization", "institution", "organizationNetBuy")
            if foreign is None and indiv is None and inst is None:
                continue
            return {"foreign": foreign, "individual": indiv, "institution": inst}
        except Exception:
            continue
    return None


# (심볼, 표시이름, 종류) — 종류: dom=국내지수, world=해외지수, fx=환율
_SPEC = [
    ("KOSPI", "코스피", "dom"),
    ("KOSDAQ", "코스닥", "dom"),
    ("NAS@IXIC", "나스닥", "world"),
    ("SPI@SPX", "S&P 500", "world"),
    ("USD_KRW", "원/달러", "fx"),
]


def fetch_indices() -> list:
    """뉴스 탭 상단 카드용 지수 목록. 항상 list 반환(부분 실패 허용)."""
    out = []
    for symbol, name, kind in _SPEC:
        item = {"name": name, "kind": kind}
        try:
            if kind == "fx":
                item.update(_fx("FX_USDKRW"))
            else:
                item.update(_basic(symbol))
            if kind == "dom":
                inv = _domestic_investors(symbol)
                if inv:
                    item["investors"] = inv
        except Exception as e:
            item["error"] = str(e)[:80]
        # 값이 하나도 없으면 스킵(카드 안 띄움)
        if item.get("value") is not None:
            out.append(item)
    return out


if __name__ == "__main__":
    data = fetch_indices()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"\n수집된 지수 {len(data)}/5")
