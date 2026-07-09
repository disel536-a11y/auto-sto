"""
주요 지수/환율 수집 (뉴스 탭 상단 카드용).
- 코스피 / 코스닥 : 지수값 + 등락 + 투자자별 순매수(외국인/개인/기관)
- 나스닥 / S&P500 : 지수값 + 등락
- 원/달러 환율      : 값 + 등락

엔드포인트(2026-07 서버 실측):
  국내지수  m.stock.naver.com/api/index/{KOSPI|KOSDAQ}/basic      (200)
  해외지수  api.stock.naver.com/index/{.IXIC|.INX}/basic          (200)
  환율/순매수는 후보 URL 목록을 순회(살아있는 것 자동 채택). fail-soft.

서버 검증/진단:
    python3 indices.py            # 수집 결과(JSON) 출력
    python3 indices.py probe      # 환율·순매수 후보 URL 원시 응답 덤프(주소 찾기용)
"""
import os
import sys
import json
import re
from datetime import datetime
import requests

_BASE = os.path.dirname(os.path.abspath(__file__))
WORLD_FILE = os.path.join(_BASE, "world_indices.json")   # 해외지수(나스닥·S&P)는 밤 cron이 갱신

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
    """'2,647.46' / 숫자 → float. 실패 시 None."""
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
        name = (str(node.get("name", "")) + str(node.get("text", ""))).upper()
        if code in ("2", "1") or "RISE" in name or "RISING" in name or "상승" in name:
            return 1
        if code in ("5", "4") or "FALL" in name or "FALLING" in name or "하락" in name:
            return -1
    return 0


def _parse_basic(j: dict) -> dict:
    """basic 응답 → {value, change(절대값), rate(절대값), dir}. change 없으면 rate로 산출."""
    value = _f(j.get("closePrice"))
    rate = _f(j.get("fluctuationsRatio"))
    d = _dir(j.get("compareToPreviousPrice"))
    change = _f(j.get("compareToPreviousClosePrice"))
    if change is not None:
        change = abs(change)
    elif value is not None and rate is not None:
        # 등락값 필드가 없으면 지수값·등락률로 역산(부호는 dir가 담당, 표시는 절대값)
        r = abs(rate) / 100.0
        change = round(value * r / (1 - r), 2) if d < 0 else round(value * r / (1 + r), 2)
    if rate is not None:
        rate = abs(rate)
    return {"value": value, "change": change, "rate": rate, "dir": d}


def _basic_domestic(code: str) -> dict:
    return _parse_basic(_get_json(f"https://m.stock.naver.com/api/index/{code}/basic"))


def _basic_world(symbol: str) -> dict:
    return _parse_basic(_get_json(f"https://api.stock.naver.com/index/{symbol}/basic"))


# 환율 URL — api.stock 이 200(데이터가 exchangeInfo 안에 있음). 나머지는 폴백.
_FX_URLS = [
    "https://api.stock.naver.com/marketindex/exchange/FX_USDKRW",
    "https://m.stock.naver.com/api/marketindex/exchange/FX_USDKRW",
]


def _fx() -> dict:
    for url in _FX_URLS:
        try:
            j = _get_json(url)
            info = j.get("exchangeInfo") if isinstance(j.get("exchangeInfo"), dict) else j
            value = _f(info.get("closePrice") or info.get("value") or info.get("dealBaseRate"))
            if value is None:
                continue
            rate = _f(info.get("fluctuationsRatio") or info.get("changeRate"))
            d = _dir(info.get("fluctuationsType") or info.get("compareToPreviousPrice"))
            change = _f(info.get("fluctuations") or info.get("compareToPreviousClosePrice") or info.get("changeValue"))
            if not d and change is not None:
                d = 1 if change > 0 else (-1 if change < 0 else 0)
            if change is not None:
                change = abs(change)
            elif value is not None and rate is not None:
                r = abs(rate) / 100.0
                change = round(value * r / (1 + r), 2)
            return {"value": value, "change": change, "rate": abs(rate) if rate is not None else None, "dir": d}
        except Exception:
            continue
    return {}


# 투자자 순매수 URL — /trend 가 200. 값 단위=억원(예: 개인 -13,278억).
def _investor_urls(code):
    return [
        f"https://m.stock.naver.com/api/index/{code}/trend",
        f"https://m.stock.naver.com/api/index/{code}/investors",
    ]


def _domestic_investors(code: str):
    for url in _investor_urls(code):
        try:
            j = _get_json(url)
            rows = j.get("investorTrends") or j.get("result") or j.get("datas") or j.get("list") or j
            if isinstance(rows, dict):
                rows = rows.get("list") or rows.get("items") or [rows]
            row = rows[0] if isinstance(rows, list) and rows else rows
            if not isinstance(row, dict):
                continue

            def pick(*keys):
                for k in keys:
                    if k in row and row[k] not in (None, ""):
                        return _f(row[k])
                return None
            # /trend 실측 키: foreignValue / personalValue / institutionalValue (억원)
            foreign = pick("foreignValue", "foreignerPureBuyQuant", "foreignerNetBuy", "foreign")
            indiv = pick("personalValue", "individualPureBuyQuant", "individualNetBuy", "individual")
            inst = pick("institutionalValue", "organizationPureBuyQuant", "organizationNetBuy", "organization")
            if foreign is None and indiv is None and inst is None:
                continue
            return {"foreign": foreign, "individual": indiv, "institution": inst}
        except Exception:
            continue
    return None


_DOMESTIC = [("KOSPI", "코스피"), ("KOSDAQ", "코스닥")]
_WORLD = [(".IXIC", "나스닥"), (".INX", "S&P 500")]


def update_world_indices() -> list:
    """해외지수(나스닥·S&P500)를 실측해 world_indices.json 에 저장.
    미국장이 열리는 밤(KST 23~06시)에 cron 으로 30분마다 호출 → 낮엔 이 파일만 읽음."""
    out = []
    for sym, name in _WORLD:
        item = {"name": name, "kind": "world"}
        try:
            item.update(_basic_world(sym))
        except Exception as e:
            item["error"] = str(e)[:80]
        if item.get("value") is not None:
            out.append(item)
    if out:   # 실패 시 기존 파일 유지(마지막 종가 보존)
        try:
            payload = {"updated": datetime.now().strftime("%Y-%m-%d %H:%M"), "indices": out}
            tmp = WORLD_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, WORLD_FILE)
        except Exception as e:
            print(f"[world] 저장 오류: {e}")
    return out


def _load_world() -> list:
    """밤 cron 이 저장한 해외지수 파일 읽기(낮엔 미국장이 닫혀 값 불변)."""
    try:
        with open(WORLD_FILE, encoding="utf-8") as f:
            return json.load(f).get("indices", [])
    except Exception:
        return []


def fetch_indices() -> list:
    """뉴스 탭 지수 목록. 순서=코스피·코스닥·(해외)·원/달러.
    국내지수+환율+순매수만 실시간 호출, 해외지수는 밤 cron 파일에서 읽음(효율)."""
    out = []
    for code, name in _DOMESTIC:
        item = {"name": name, "kind": "dom"}
        try:
            item.update(_basic_domestic(code))
            inv = _domestic_investors(code)
            if inv:
                item["investors"] = inv
        except Exception as e:
            item["error"] = str(e)[:80]
        if item.get("value") is not None:
            out.append(item)
    out += _load_world()   # 해외지수(밤 cron 갱신)
    fx = {"name": "원/달러", "kind": "fx"}
    try:
        fx.update(_fx())
    except Exception as e:
        fx["error"] = str(e)[:80]
    if fx.get("value") is not None:
        out.append(fx)
    return out


def _probe():
    """환율·투자자 후보 URL 원시 응답 덤프(주소 찾기용)."""
    urls = list(_FX_URLS) + _investor_urls("KOSPI")
    for u in urls:
        try:
            r = requests.get(u, headers=_HEADERS, timeout=_TIMEOUT)
            print(f"\n=== {u} → {r.status_code}")
            print(r.text[:700])
        except Exception as e:
            print(f"\n=== {u} → ERR {e}")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "probe":
        _probe()
    elif arg == "world":
        # 밤 cron 용: 해외지수만 실측해 world_indices.json 저장
        w = update_world_indices()
        print(json.dumps(w, ensure_ascii=False, indent=2))
        print(f"\n해외지수 저장 {len(w)}/2 → {WORLD_FILE}")
    else:
        data = fetch_indices()
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"\n수집된 지수 {len(data)}")
