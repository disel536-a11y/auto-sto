"""
주식 모니터링 봇 (티마식 테마 분석 + 상한가 알림)
- 네이버 금융에서 거래대금/상승률 종목 수집
- 거래대금 상위 종목(대장주) 중 상승률 10%+ 를 시드로 테마 자동 분류 (Gemini LLM)
- 상승률 상위 종목 중 동일 테마를 묶어 상승률 순 정렬
- 상한가 종목도 테마 + 뉴스 요약을 붙여서 알림
- 카카오톡 "나에게 보내기"로 전송

실행: python stock_monitor.py
점검: python stock_monitor.py --selftest   (네트워크/LLM 없이 포맷·로직 점검)
"""

import os
import json
import time
import traceback
import re
import sys
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# 지수/환율 수집(뉴스 탭 상단 카드). 없거나 실패해도 봇은 정상 동작(fail-soft).
try:
    import indices as _indices_mod
except Exception:
    _indices_mod = None
_indices_cache = []       # 최근 수집한 지수 목록
_indices_ts = 0.0         # 마지막 수집 시각
INDEX_INTERVAL = 120      # 지수 갱신 주기(초) — 지수는 자주 안 변해 2분이면 충분


def _get_indices_cached() -> list:
    """지수 목록을 INDEX_INTERVAL 간격으로만 갱신해서 반환(과도한 네트워크 방지)."""
    global _indices_cache, _indices_ts
    if _indices_mod is None:
        return _indices_cache
    if time.time() - _indices_ts >= INDEX_INTERVAL or not _indices_cache:
        try:
            data = _indices_mod.fetch_indices()
            if data:
                _indices_cache = data
            _indices_ts = time.time()
        except Exception as e:
            print(f"  [indices] 수집 오류(무시): {e}")
    return _indices_cache

# ══════════════════════════════════════════════════
#  ✏️  설정 — 여기만 수정하면 됩니다
# ══════════════════════════════════════════════════

# 비밀 키(제미나이/카카오)는 config.py 에서 읽습니다 — git 에는 올리지 않습니다.
# config.py 가 없으면 환경변수에서 읽습니다. (config.example.py 를 복사해 config.py 생성)
try:
    import config as _config
except ImportError:
    _config = None

def _cfg(name, default=""):
    if _config is not None and hasattr(_config, name):
        return getattr(_config, name)
    return os.environ.get(name, default)

# ── 카카오 ─────────────────────────────────────────
# 본인 토큰은 kakao_token.json(자동 갱신)에서 읽습니다.
#   → 최초 1회 'python kakao_token_setup.py' 실행해서 kakao_token.json 생성
KAKAO_REST_API_KEY = _cfg("KAKAO_REST_API_KEY")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KAKAO_TOKEN_FILE = os.path.join(_BASE_DIR, "kakao_token.json")       # 본인
KAKAO_FRIENDS_FILE = os.path.join(_BASE_DIR, "kakao_friends.json")   # 친구들(자동 갱신)
DATA_FILE = os.path.join(_BASE_DIR, "data.json")                     # 대시보드용 최신 결과
REFRESH_FLAG = os.path.join(_BASE_DIR, "refresh.flag")               # 웹 새로고침 버튼 신호(봇 루프가 감지)
HEARTBEAT_FILE = os.path.join(_BASE_DIR, "bot.heartbeat")            # 봇 루프 생존 신호(웹서버가 확인)
REFRESH_LOCK = os.path.join(_BASE_DIR, "refresh.lock")              # 장외 --once 중복 실행 방지 락
UPPER_TS_FILE = os.path.join(_BASE_DIR, "upper_first.json")         # 종목별 최초 상한가 진입 시각(오늘자, 조건부합 대표 선정용)
KAKAO_TOKENS = [
    # "친구_access_token_여기에",  # (레거시) 자동갱신 안 됨. 친구는 kakao_add_friend.py 사용 권장
]

# 카카오 알림 사용 여부 — False 면 카톡 전송 안 함(대시보드만 사용). 다시 켜려면 True.
KAKAO_ENABLED = False

# ── Gemini (Google AI Studio) API 키 ────────────────
GEMINI_API_KEY = _cfg("GEMINI_API_KEY")
# ── LLM 모델 체인 (2026-09-15 개편) ──────────────────────────
#   무료 일일한도(RPD)는 '프로젝트 × 모델' 단위로 따로 잡힌다.
#   (429 응답의 quotaId = GenerateRequestsPerDayPerProjectPerModel-FreeTier)
#   → 한 모델이 소진돼도 다른 모델은 자기 몫이 남아 있다. 체인으로 순회한다.
#   · gemma 계열이 무료 한도가 가장 커서 1순위. 2.5-flash-lite 는 하루 20콜뿐이라
#     (2026-09-15 실측) 더 이상 주력으로 못 쓴다 → 예비로 내림.
#   · thinking=True 면 thinkingConfig{thinkingBudget:0} 을 보내 추론을 끈다(속도↑, 추론 누출 방지).
#     thinking=False 는 그 필드를 아예 안 보내는 모델용. 400 이 나면 런타임에 학습해 뺀다.
#   순서를 바꾸고 싶으면 이 리스트만 고치면 된다.
GEMINI_MODELS = [
    #   gemma-4 는 추론(thinking) 모델인데 thinkingConfig 로 끄는 것을 API 가 거부한다(400, 실측).
    #   그래서 추론 과정이 text 로 새어 나온다(thoughtsTokenCount ~2700) — 이건 _salvage_json 이
    #   '마지막 JSON 블록' 을 고르는 방식으로 흡수한다. thinking=False 로 두어 헛호출을 막는다.
    {"name": "gemma-4-26b-a4b-it",    "thinking": False},
    {"name": "gemini-2.5-flash-lite", "thinking": True},
    {"name": "gemini-2.5-flash",      "thinking": True},
]

# 상한가 알림 기준 등락률 (%)
UPPER_LIMIT_THRESHOLD = 29.0

# ── 티마식 테마 분석 파라미터 ───────────────────────
TRANS_TOP_N   = 15    # 거래대금 상위 N개(대장주 후보) — 삼성전자/SK하이닉스/ETF/ETN 제외 후
SEED_MIN_RATE = 10.0  # 시드 종목 최소 상승률 (%)
RISE_TOP_N    = 20    # 상승률 상위 N개 (동일 테마 편입 대상)

# 시세 스캔 주기 (초)  →  60 = 1분 (상한가 목록 + 테마 종목 시세를 매분 갱신)
SCAN_INTERVAL = 60

# 테마 LLM 재분류 주기 (초) → 600 = 10분
#   그 사이 스캔에서는 Gemini 호출 없이 기존 테마 종목의 시세만 갱신 → 무료 할당량 보호.
#   장중 6.5시간 기준 하루 약 39사이클(사이클당 2~3콜) → 무료 분당 한도(15 RPM) 여유 확보.
#   (2026-07-07: 5분→10분. flash-lite 무료 분당 한도 초과 429로 테마가 폴백되던 문제 완화.
#    가격/상승률/상한가는 SCAN_INTERVAL(1분)로 계속 갱신되고, '새 테마 인지'만 최대 10분 지연.)
THEME_LLM_INTERVAL = 600

# ── 조건부합(대시보드 테마탭 최상단) 파라미터 ──────────
#   각 테마의 상승률 1위(=대장)가 대금 하한을 넘고, 같은 테마에 동조 상승 종목(2등주)이
#   있으면 후보. 상승률순 상위 MATCH_MAX개만 표시. 대금이 HL 이상이면 빨간 테두리 강조.
MATCH_AMOUNT_MIN_EOK = 1000   # (테마) 대금 형성 최소 거래대금(억) — 동조주 있을 때
MATCH_AMOUNT_HL_EOK  = 3000   # 이 값 이상이면 빨간 테두리 강조(억). '단독 급등' 노출 하한도 이 값.
MATCH_SOLO_RATE_MIN  = 20.0   # 동조주 없이 혼자 급등한 종목을 조건부합에 띄우는 최소 상승률(%)
MATCH_MAX            = 3      # 최대 표시 종목 수

# 장 운영 시간 (분 단위, KST)
#   2026-09 KRX 정규장 마감 연장(15:30 → 20:00) 반영.
#   상한가 판정 / 마감 N상 집계 로직은 그대로 두고 '봇 가동 시간'만 넓힌다.
MARKET_PRE_MIN   = 8 * 60      # 08:00  장 전 준비
MARKET_OPEN_MIN  = 9 * 60      # 09:00  개장
MARKET_CLOSE_MIN = 20 * 60     # 20:00  마감 → 이 시각 이후 오늘 세션 종료

# ══════════════════════════════════════════════════

NAVER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.naver.com/sise/",
    "Accept-Language": "ko-KR,ko;q=0.9",
}
SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

# 제외 종목 (ETF/ETN 운용사 키워드 + 고정 종목)
ETF_KEYWORDS = ["KODEX", "TIGER", "ARIRANG", "KINDEX", "HANARO", "KBSTAR",
                "KOSEF", "SOL", "ACE", "PLUS", "TIMEFOLIO", "TREX", "RISE",
                "ETF", "ETN", "인버스", "레버리지", "선물"]
EXCLUDE_NAMES = {"삼성전자", "SK하이닉스"}


# ─── 네이버 금융 크롤링 ───────────────────────────

def _parse_trading_value(text: str) -> int:
    """'1,234억' or '1.2조' → 원 단위 int"""
    text = text.replace(',', '').strip()
    if '조' in text:
        return int(float(text.replace('조', '')) * 1_000_000_000_000)
    elif '억' in text:
        return int(float(text.replace('억', '')) * 100_000_000)
    return 0


def _parse_naver_page(url: str, market: str, from_trans: bool = False) -> list:
    """네이버 시세 페이지에서 종목 행을 파싱. 페이지 정렬 순서를 'rank'로 보존."""
    results = []
    try:
        res = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        res.encoding = 'euc-kr'
        html = res.text

        rank = 0
        for row in html.split('<tr'):
            code_m = re.search(r'code=(\d{6})', row)
            if not code_m:
                continue
            name_m = re.search(r'code=\d{6}"[^>]*>([^<]+)</a>', row)
            if not name_m:
                continue
            name = name_m.group(1).strip()
            if not name:
                continue

            price_m = re.search(r'class="nv">([0-9,]+)', row) or \
                      re.search(r'class="number[^"]*">\s*([0-9,]+)\s*</td>', row)
            price = int(price_m.group(1).replace(',', '')) if price_m else 0

            rate_m = re.search(r'([+\-][0-9]+\.[0-9]+)%', row)
            rate = float(rate_m.group(1)) if rate_m else 0.0

            # 거래대금 파싱
            trdval = 0
            val_m = re.search(r'<strong>([0-9,]+(?:\.[0-9]+)?(?:억|조))</strong>', row)
            if val_m:
                trdval = _parse_trading_value(val_m.group(1))
            if not trdval:
                val_m = re.search(r'>([0-9,]+(?:\.[0-9]+)?(?:억|조))<', row)
                if val_m:
                    trdval = _parse_trading_value(val_m.group(1))
            if not trdval:
                nums = re.findall(
                    r'<td[^>]+class="[^"]*number[^"]*"[^>]*>\s*([0-9,]+)\s*</td>', row
                )
                if from_trans and len(nums) >= 6:
                    try:
                        val_raw = int(nums[5].replace(',', ''))
                        if val_raw > 0:
                            trdval = val_raw * 1_000_000  # 백만원 → 원
                    except (ValueError, IndexError):
                        pass

            rank += 1
            # 거래대금 페이지인데 값 파싱 실패 시 순위 기반 추정 (정렬 순서만 보존)
            if not trdval and from_trans:
                trdval = max(1, (300 - rank) * 50_000_000_000)

            results.append({
                "코드":     code_m.group(1),
                "종목명":   name,
                "시장":     market,
                "종가":     price,
                "등락률":   rate,
                "거래대금": trdval,
                "순위":     rank,   # 페이지 내 정렬 순위 (거래대금 페이지는 거래대금 내림차순)
            })
    except Exception as e:
        print(f"  [네이버/{market}] 오류: {e}")
    return results


# ─── 네이버 모바일 시세 API (2026-09-11: PC 금융 시세표 폐지 대응) ───
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
        t = re.sub(r'[^0-9.\-]', '', str(v))
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
            if not re.fullmatch(r"\d{6}", code) or not name:
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


def _parse_quant_page(url: str, market: str) -> list:
    """
    네이버 시세 리스트 페이지(sise_quant / sise_rise 등) 파싱.
    헤더(th)로 컬럼 위치 자동 감지. number 클래스 td 는 '현재가'(th 3번째)부터 → num_idx = th_idx - 2.
    거래대금=백만원→원, 시가총액=억원.
    """
    results = []
    try:
        res = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        res.encoding = 'euc-kr'
        html = res.text

        ths = [re.sub(r'<[^>]+>', '', t).strip()
               for t in re.findall(r'<th[^>]*>(.*?)</th>', html, re.S)]
        ths = [t for t in ths if t]

        def col(label, default):
            if label in ths:
                idx = ths.index(label) - 2   # N, 종목명 두 컬럼 제외
                return idx if idx >= 0 else default
            return default

        i_price = col('현재가', 0)
        i_rate  = col('등락률', 2)
        i_amt   = col('거래대금', 4)
        i_cap   = col('시가총액', 7)

        for row in html.split('<tr'):
            code_m = re.search(r'code=(\d{6})', row)
            if not code_m:
                continue
            name_m = re.search(r'code=\d{6}"[^>]*>([^<]+)</a>', row)
            if not name_m or not name_m.group(1).strip():
                continue
            nums = [re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', n)).strip()
                    for n in re.findall(r'<td[^>]*class="[^"]*number[^"]*"[^>]*>(.*?)</td>', row, re.S)]
            nums = [n for n in nums if n]
            if len(nums) <= i_rate:
                continue

            def g(i):
                return nums[i] if 0 <= i < len(nums) else ''

            price = int(re.sub(r'[^0-9]', '', g(i_price)) or 0)
            rm = re.search(r'([+\-]?[0-9.]+)%', g(i_rate))
            rate = float(rm.group(1)) if rm else 0.0
            amt_man = int(re.sub(r'[^0-9]', '', g(i_amt)) or 0)   # 백만원
            cap_eok = int(re.sub(r'[^0-9]', '', g(i_cap)) or 0)   # 억원

            results.append({
                "코드":     code_m.group(1),
                "종목명":   name_m.group(1).strip(),
                "시장":     market,
                "종가":     price,
                "등락률":   rate,
                "거래대금": amt_man * 1_000_000,   # 백만원 → 원
                "시총억":   cap_eok,               # 억원
                "순위":     0,
            })
    except Exception as e:
        print(f"  [네이버/{market}] 오류: {e}")
    return results


# ─── ETF / 고정 종목 필터 ─────────────────────────

def is_etf(name: str) -> bool:
    name_upper = name.upper()
    return any(kw.upper() in name_upper for kw in ETF_KEYWORDS)


def is_excluded(name: str) -> bool:
    return is_etf(name) or name in EXCLUDE_NAMES


# ─── 네이버 뉴스 검색 ─────────────────────────────

def _clean_html(t: str) -> str:
    t = re.sub(r'<[^>]+>', '', t)
    t = (t.replace("&quot;", '"').replace("&amp;", "&").replace("&lt;", "<")
           .replace("&gt;", ">").replace("&#39;", "'").replace("&nbsp;", " ")
           .replace("&hellip;", "…").replace("&middot;", "·").replace("&apos;", "'"))
    return re.sub(r'\s+', ' ', t).strip()


def get_news_headlines(code: str, n: int = 8) -> list:
    """종목코드로 네이버 '모바일' 뉴스 API 에서 최신 제목 n개 반환.

    (2026-09-18) PC 페이지 finance.naver.com/item/news_news.naver 가 HTTP 410(폐지)으로
    죽어 있었다 — 결과가 빈 리스트라 예외도 안 나고 조용히 모든 종목이 '관련 뉴스 없음' 이
    됐고, 그 결과 LLM 이 대부분을 '개별 등락' 으로 묶었다. 시세표(9/11)와 같은 전환.

    m.stock.naver.com/api/news/stock/{code}?pageSize=N&page=1 응답 형태:
      [ {"total": k, "items": [ {"title", "titleFull", "body", "datetime", "officeName",
                                 "mobileNewsUrl", ...}, ... ]},   # 유사 기사 묶음(cluster)
        ... ]
    묶음마다 대표 1건씩 뽑아 중복 없이 n개. title 은 '...' 로 잘리므로 titleFull 우선."""
    try:
        url = (f"https://m.stock.naver.com/api/news/stock/{code}"
               f"?pageSize={max(n, 10)}&page=1")
        res = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        if res.status_code != 200:
            print(f"  [뉴스] {code} HTTP {res.status_code}")
            return []
        return _parse_mobile_news(res.json(), n)
    except Exception as e:
        print(f"  [뉴스] {code} 조회 오류: {e}")
        return []


def _parse_mobile_news(data, n: int) -> list:
    """모바일 뉴스 API 응답 → 제목 리스트. 묶음 구조/평면 구조 모두 수용."""
    if isinstance(data, dict):            # 혹시 {"items":[...]} 로 감싸 오는 경우
        data = data.get("items") or []
    out, seen = [], set()
    for cl in data or []:
        if not isinstance(cl, dict):
            continue
        items = cl.get("items")
        if items is None:                 # 묶음이 아니라 기사 자체인 경우
            items = [cl]
        for it in items or []:
            if not isinstance(it, dict):
                continue
            title = _clean_html((it.get("titleFull") or it.get("title") or "").strip())
            if title and title not in seen:
                seen.add(title)
                out.append(title[:120])
                break                     # 묶음당 대표 1건(유사 기사 중복 방지)
        if len(out) >= n:
            break
    return out


# ─── Gemini LLM ───────────────────────────────────

def _gemini_ready() -> bool:
    return bool(GEMINI_API_KEY) and "붙여넣기" not in GEMINI_API_KEY


def _extract_text(resp: dict) -> str:
    """응답의 모든 part 텍스트를 이어 붙여 본문을 만든다(parts[0] 하나만 읽지 않는다).
    Gemini 계열 추론 모델이 includeThoughts 로 사고 과정을 별도 part(thought=true)로 줄 때는
    그 part 를 제외한다. (gemma-4 는 사고 과정을 일반 text 에 섞어 보내므로 여기서 걸러지지
    않고 _salvage_json 의 '마지막 블록 선택' 으로 처리된다.)"""
    cand = (resp.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    out = []
    for pt in parts:
        if pt.get("thought"):          # 추론 과정 part 는 본문이 아니다
            continue
        t = pt.get("text")
        if t:
            out.append(t)
    if out:
        return "".join(out)
    # 전부 thought 로 표시된 경우엔 어쩔 수 없이 텍스트가 있는 part 라도 쓴다
    return "".join(pt.get("text") or "" for pt in parts)


def _salvage_json(txt: str):
    """Gemini 응답 텍스트를 최대한 관대하게 JSON 파싱.
    1) 그대로 파싱 → 2) 마크다운 펜스 제거 후 파싱 →
    3) 본문 속 균형 잡힌 {...} 블록들 중 '마지막으로 파싱되는 것' 선택(서두 산문·후행 잡음·
       추론 누출 속 스키마 되뇜 무시). 중간에 잘린 JSON 은 구제하지 않는다(파서가 못 읽음).
    실패하면 None."""
    if not txt:
        return None
    txt = txt.strip()

    # strict=False → 문자열 안의 '진짜 줄바꿈' 같은 제어문자를 허용한다.
    #   (2026-09-18) gemma 가 요약을 이스케이프 대신 실제 개행으로 내보내
    #   finishReason=STOP 인데도 파싱이 전부 실패하던 원인. 기본 json.loads 는 이를 거부한다.
    def _loads(t):
        try:
            return json.loads(t, strict=False)
        except Exception:
            pass
        t2 = re.sub(r",\s*([}\]])", r"\1", t)   # 후행 콤마 구제
        if t2 != t:
            try:
                return json.loads(t2, strict=False)
            except Exception:
                pass
        return None

    obj = _loads(txt)
    if obj is not None:
        return obj
    # ```json ... ``` 펜스 제거
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?", "", txt).strip()
        txt = re.sub(r"```$", "", txt).strip()
        obj = _loads(txt)
        if obj is not None:
            return obj
    # 본문 안의 균형 잡힌 {...} 블록을 모두 찾아 파싱되는 것 중 '마지막' 을 택한다.
    #   (2026-09-18) 추론 모델은 답 앞에 사고 과정을 늘어놓고 그 안에서 스키마를 되뇐다 —
    #   그 빈 스키마도 유효한 JSON 이라 '첫 번째' 블록을 잡으면 그걸 정답으로 오인한다.
    #   실제 답은 맨 뒤에 오므로 마지막으로 파싱되는 블록을 쓴다.
    best = None
    i, n = 0, len(txt)
    while True:
        start = txt.find("{", i)
        if start < 0:
            break
        depth, in_str, esc, end = 0, False, False, -1
        for j in range(start, n):
            c = txt[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        end = j
                        break
        if end < 0:
            break                     # 닫히지 않은 블록 — 더 볼 것 없음
        obj = _loads(txt[start:end + 1])
        if obj is not None:
            best = obj
        i = end + 1
    return best


# ── Gemini 호출 레이트 제어(무료 분당 한도 보호) ───────────────
#   무료 flash-lite는 분당 요청 한도(RPM ~15)가 낮아, 한 스캔에서 콜이 몰리면 429가 나고
#   재시도가 폭주하면 그날 남은 호출까지 태워 테마가 하루 종일 폴백된다. 이를 막기 위해:
#   ① 콜 간 최소 간격(_GEMINI_MIN_GAP)을 둬 분당 한도 밑으로 유지,
#   ② 429가 뜨면 그 자리에서 재시도하지 않고 _GEMINI_COOLDOWN 동안 모든 Gemini 콜을 전면
#      중단(서킷브레이커) → 분당 버킷이 회복될 시간을 확보. (테마는 keep-previous로 유지)
_GEMINI_MIN_GAP  = 4.5    # 콜 간 최소 간격(초) — 분당 한도 보호(전 모델 공통)
_GEMINI_COOLDOWN = 90.0   # 429 1회째 쿨다운(초)
#   429 가 연속되면 그 모델의 쿨다운을 2배씩 늘린다(90→180→…, 상한 6시간).
#   분당 한도면 1~2회로 풀리고, 일일 한도 소진이면 길게 재워 다음 모델에 순번을 넘긴다.
#   (일일 한도는 태평양 자정 = KST 16:00~17:00 에 리셋된다)
_GEMINI_COOLDOWN_MAX = 6 * 3600.0
_gemini_last_call = 0.0
_model_block  = {}    # 모델명 -> 이 시각까지 호출 건너뜀
_model_429    = {}    # 모델명 -> 연속 429 횟수
_no_thinking  = set() # thinkingConfig 로 400 을 낸 모델(런타임 학습)
_no_jsonmode  = set() # responseMimeType 으로 400 을 낸 모델(런타임 학습)


def _gemini_once(spec: dict, prompt: str, retries: int):
    """모델 하나로 호출 시도. 성공 시 파싱된 객체, 실패/한도면 None."""
    global _gemini_last_call
    model = spec["name"]
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={GEMINI_API_KEY}")
    parse_fail = 0    # 파싱 실패 재시도 횟수(한도 절약을 위해 1회로 제한)
    for _ in range(retries + 1):
        cfg = {"temperature": 0.2, "maxOutputTokens": 8192}
        if model not in _no_jsonmode:
            cfg["responseMimeType"] = "application/json"
        if spec.get("thinking") and model not in _no_thinking:
            cfg["thinkingConfig"] = {"thinkingBudget": 0}   # 사고모드 OFF → 속도↑
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}

        gap = _GEMINI_MIN_GAP - (time.time() - _gemini_last_call)
        if gap > 0:
            time.sleep(gap)
        _gemini_last_call = time.time()
        try:
            r = requests.post(url, json=body, timeout=120)
            if r.status_code == 200:
                txt = _extract_text(r.json())
                obj = _salvage_json(txt)
                if obj is not None:
                    _model_429[model] = 0          # 정상 응답 → 백오프 해제
                    if model != GEMINI_MODELS[0]["name"]:
                        # 1순위가 아닌 모델이 받아냈다 = 폴백 중. 무슨 일인지 보이게 남긴다.
                        print(f"  [LLM/{model}] 폴백 응답(1순위 {GEMINI_MODELS[0]['name']} 실패)")
                    return obj
                # 파싱 실패는 1회만 재시도한다. 같은 모델이 계속 깨진 JSON 을 뱉는 경우
                # 재시도가 하루 한도를 태울 뿐이라, 빨리 다음 모델로 넘기는 편이 낫다.
                parse_fail += 1
                fr = (r.json().get("candidates") or [{}])[0].get("finishReason")
                print(f"  [LLM/{model}] JSON 파싱 실패 (finishReason={fr}, len={len(txt)})"
                      + (" — 재시도" if parse_fail < 2 else " — 다음 모델로"))
                if parse_fail >= 2:
                    # 추측하지 않도록 원문 앞부분을 남긴다(개행은 한 줄로 접어서).
                    print("    원문: " + txt[:400].replace("\n", "\\n"))
                    if not txt:
                        # 텍스트가 비면 응답 구조 자체를 봐야 한다(part 분리/필터링 등).
                        cand = (r.json().get("candidates") or [{}])[0]
                        print("    응답구조: " + json.dumps(cand, ensure_ascii=False)[:600])
                if parse_fail >= 2:
                    return None
                time.sleep(2)
            elif r.status_code == 429:
                n = min(_model_429.get(model, 0) + 1, 8)
                _model_429[model] = n
                cd = min(_GEMINI_COOLDOWN * (2 ** (n - 1)), _GEMINI_COOLDOWN_MAX)
                _model_block[model] = time.time() + cd
                # 응답 본문에 quotaId 가 있다(PerDay vs PerMinute 구분용) → 로그에 남긴다.
                print(f"  [LLM/{model}] 한도(429) x{n} — {int(cd)}초 차단 | {r.text[:180]}")
                return None
            elif r.status_code == 400:
                # 미지원 필드 자동 학습: 한 번 빼보고 재시도, 그래도 400 이면 포기.
                body_txt = r.text[:300]
                if "thinking" in body_txt.lower() and model not in _no_thinking:
                    _no_thinking.add(model)
                    print(f"  [LLM/{model}] thinkingConfig 미지원 — 제외하고 재시도")
                    continue
                if ("responseMimeType" in body_txt or "response_mime_type" in body_txt) \
                        and model not in _no_jsonmode:
                    _no_jsonmode.add(model)
                    print(f"  [LLM/{model}] JSON 모드 미지원 — 프롬프트 지시로만 재시도")
                    continue
                print(f"  [LLM/{model}] HTTP 400: {body_txt}")
                return None
            else:
                print(f"  [LLM/{model}] HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(3)
        except Exception as e:
            print(f"  [LLM/{model}] 오류: {e}")
            time.sleep(3)
    return None


def gemini_json(prompt: str, retries: int = 2):
    """모델 체인을 순회하며 JSON 응답을 받아온다. 전부 실패하면 None.
    한도(429)로 차단된 모델은 쿨다운이 풀릴 때까지 건너뛴다."""
    if not _gemini_ready():
        return None
    now = time.time()
    avail = [m for m in GEMINI_MODELS if _model_block.get(m["name"], 0.0) < now]
    if not avail:
        return None          # 전 모델 차단 중 — 조용히 포기(테마는 keep-previous)
    for spec in avail:
        obj = _gemini_once(spec, prompt, retries)
        if obj is not None:
            return obj
    return None


# ─── 상한가 종목 테마/요약 분류 ────────────────────

def classify_upper(stocks: list) -> dict:
    """
    상한가 종목들에 대해 테마 + 1줄 뉴스 요약 생성.
    반환: {코드: {"theme": str, "summary": str}}
    """
    if not stocks or not _gemini_ready():
        return {}

    # 뉴스 수집
    blocks = []
    for s in stocks:
        heads = get_news_headlines(s["코드"], n=6)
        time.sleep(0.4)
        joined = " / ".join(heads) if heads else "(관련 뉴스 없음)"
        blocks.append(f'코드 {s["코드"]} | {s["종목명"]} (+{s["등락률"]}%)\n  뉴스: {joined}')

    prompt = (
        "너는 한국 주식 전문 애널리스트다. 아래 상한가/급등 종목들의 최신 뉴스 헤드라인을 보고, "
        "각 종목이 왜 올랐는지 테마와 핵심 이유를 분석하라.\n"
        "각 종목마다 (1) theme: 시장에서 통용되는 간결한 테마명(예: 원전, 2차전지, 로봇, 우크라이나 재건, 정치인테마 등), "
        "(2) summary: 상승 이유를 담은 한 줄(최대 45자) 요약 을 만들어라.\n"
        "반드시 아래 JSON 형식으로만 답하라:\n"
        '{"items":[{"code":"종목코드","theme":"테마명","summary":"한줄요약"}, ...]}\n\n'
        "종목 목록:\n" + "\n".join(blocks)
    )
    data = gemini_json(prompt)
    out = {}
    for it in _llm_items(data, "items"):
        code = str(it.get("code", "")).strip()
        if code:
            out[code] = {
                "theme": (it.get("theme") or "").strip(),
                "summary": (it.get("summary") or "").strip(),
            }
    return out


# ─── 특징주(종목별) 상세 분석 ──────────────────────

def _siztext(cap_eok: int) -> str:
    if not cap_eok or cap_eok <= 0:
        return ""
    if cap_eok >= 10000:
        return f"시총{cap_eok/10000:.1f}조"
    hund = (cap_eok // 100) * 100
    return f"시총{hund}억대" if hund >= 100 else f"시총{cap_eok}억"


# 종목 상세정보 세션 캐시(증분 분석용): code -> {업종,사업,특이사항,핵심재료}
#   봇은 매일 09:00 재시작(cron)되어 새로 비워지므로 하루 내에서만 재사용.
_stock_info_cache = {}

# 상세분석 실패 종목의 마지막 시도 시각: code -> ts
#   실패해도 캐시에 안 들어가 매 스캔(60s) 같은 종목을 다시 호출하던 문제를 막는다.
#   한도가 빡빡할 때 이 재호출이 테마 묶기의 호출 예산을 갉아먹는 주범이었다.
_enrich_failed = {}
_ENRICH_RETRY_GAP = 1800.0   # 실패 종목 재시도 최소 간격(초) = 30분


def _llm_enrich(stocks: list, news_map: dict) -> dict:
    """[증분] 아직 상세정보가 없는 종목만 상세 분석. 반환 {code: info}, 실패 시 {}.
    전체를 매번 한꺼번에 처리하지 않고 '새로 등장한 종목'만 보내므로 응답이 작아
    잘림/실패가 줄고, 실패해도 테마 묶기(구조)는 별도로 살아남는다."""
    if not stocks or not _gemini_ready():
        return {}
    blocks = []
    for s in stocks:
        nlines = "\n    ".join("· " + h for h in news_map.get(s["코드"], [])) or "· (관련 뉴스 없음)"
        blocks.append(
            f'[{s["코드"]}] {s["종목명"]} (+{s["등락률"]}%, 시총 {s.get("시총억", 0)}억)\n    {nlines}'
        )
    prompt = (
        "너는 한국 증시 애널리스트다. 아래 급등 종목 각각의 최신 뉴스를 보고 종목 정보를 정리하라. "
        "뉴스에 없는 사실은 지어내지 마라.\n"
        "- code: 종목코드\n"
        "- 업종: 'OO관련주' (예: 기계관련주, 화장품관련주, 이차전지관련주, 건설관련주, 레저관련주)\n"
        "- 사업: 핵심 사업 명사구 (예: 도로안전시설물사업, 골프장사업)\n"
        "- 특이사항: 뉴스에 명시된 것만 배열 (신규상장/투자주의종목/투자환기종목/관리종목/자금조달 진행 중/"
        "CB발행 결정/유상증자 결정/최대주주 변경/공개매수/상장폐지 추진 등). 없으면 [].\n"
        "- 핵심재료: 상승 핵심 재료를 구체적 수치/계약/정책을 살려 1~2문장. 뚜렷한 재료 없으면 '개별 등락'.\n\n"
        "반드시 JSON 으로만:\n"
        '{"items":[{"code":"","업종":"","사업":"","특이사항":[],"핵심재료":""}]}\n\n'
        "종목 목록:\n" + "\n\n".join(blocks)
    )
    data = gemini_json(prompt)
    out = {}
    for it in _llm_items(data, "items"):
        c = str(it.get("code", "")).strip()
        if c:
            out[c] = it
    return out


def _join_summary(v) -> str:
    """요약 필드를 문자열로 정규화. 모델이 배열로 줄 수도, 한 문자열로 줄 수도 있다."""
    if isinstance(v, (list, tuple)):
        return "\n".join(str(x).strip() for x in v if str(x).strip())
    return v.strip() if isinstance(v, str) else ""


def _llm_items(data, key: str) -> list:
    """LLM 응답에서 리스트를 꺼낸다. {"key":[...]} 든, 바깥 껍데기 없는 [...] 든 수용.
    (2026-09-18) gemma 가 {"items":[...]} 대신 [...] 를 돌려줘 .get 에서 봇이 죽은 사고 방지.
    원소는 dict 만 남긴다."""
    if isinstance(data, dict):
        v = data.get(key)
        items = v if isinstance(v, list) else []
    elif isinstance(data, list):
        items = data
    else:
        items = []
    return [it for it in items if isinstance(it, dict)]


def _clip_grade(v) -> int:
    """재료 인지도 등급을 1~5 정수로 정규화(파싱 실패/범위밖이면 1)."""
    try:
        return max(1, min(5, int(round(float(v)))))
    except (TypeError, ValueError):
        return 1


def _llm_group_themes(uni: list, news_map: dict, seed_codes: set):
    """급등 종목을 '테마로 묶기'만 수행(응답이 짧아 잘림/실패에 강함).
    반환: [{"테마","요약","codes":[...]}, ...]  또는 실패 시 None."""
    if not _gemini_ready():
        return None
    blocks = []
    for s in uni:
        tag = " [거래대금대장주]" if s["코드"] in seed_codes else ""
        heads = news_map.get(s["코드"], [])[:3]
        nl = " / ".join(heads) if heads else "(관련 뉴스 없음)"
        sect = _stock_info_cache.get(s["코드"], {}).get("업종", "")
        sect = f" {sect}" if sect else ""
        blocks.append(f'[{s["코드"]}] {s["종목명"]} (+{s["등락률"]}%){sect}{tag} :: {nl}')
    prompt = (
        "너는 한국 증시 테마 분석가다. 아래 급등 종목들을 같은 재료/테마끼리 묶어라. "
        "뉴스에 없는 사실은 지어내지 마라.\n"
        "- 테마: 시장 통용 테마명 (예: 원전, 이차전지, 로봇, 호남 반도체 클러스터(지역), 신규상장 등)\n"
        "- 요약: 그 테마가 오늘 부각된 핵심 뉴스. 문자열 2개짜리 배열(각 40자 이내). 줄바꿈 문자는 쓰지 마라\n"
        "- codes: 그 테마에 속하는 종목코드 배열 (같은 재료면 반드시 함께 묶어라)\n"
        "- 등급: 그 테마 '재료(뉴스)'가 얼마나 널리 알려졌는지 1~5 정수. 재료를 아는 청중의 크기로 판단하라.\n"
        "    1 = 뚜렷한 뉴스/재료 없이 오름 (수급/차트만)\n"
        "    2 = 그 종목 주주·관계자 정도만 아는 개별기업 정보 (실적 개선, 유상증자, 단일 수주공시 등)\n"
        "    3 = 주식 좀 하는 트레이더층이 아는 재료 (그룹사 지분매각, 부품 생태계 편입, ADR 편입 등 섹터 이슈)\n"
        "    4 = 일반 뉴스(9시뉴스)에 나올 화제성 (반도체 메가프로젝트, 유럽 폭염, 우크라이나 재건, 초전도체·양자컴 등 신기술)\n"
        "    5 = 뉴스를 안 봐도 대중이 아는 대형 사건 (코로나, 대선, 러시아-우크라이나 전쟁 등)\n"
        "  애매하면 재료의 '대중 인지도'가 더 넓은 쪽으로 한 단계 올려 판단하라.\n"
        "규칙: 같은 정책/이슈(예: 동일 지역개발, 동일 정책 수혜)로 오른 종목은 하나의 테마로 묶는다. "
        "뚜렷한 공통 재료 없이 혼자 오른 종목들은 '개별 등락'으로 묶어라(등급 1). "
        "거래대금대장주가 포함된 테마를 우선한다. 한 종목은 한 테마에만.\n\n"
        "반드시 JSON 으로만:\n"
        '{"themes":[{"테마":"","요약":["1줄","2줄"],"등급":3,"codes":["",""]}]}\n\n'
        "종목 목록:\n" + "\n".join(blocks)
    )
    data = gemini_json(prompt)
    themes = _llm_items(data, "themes")   # {"themes":[...]} 또는 바깥 껍데기 없는 [...] 모두 수용
    if not themes:
        return None
    # 검증: 종목코드가 실제로 든 테마가 최소 하나는 있어야 한다.
    #   (2026-09-18) 모델이 스키마만 되뇐 빈 껍데기 {"테마":"","codes":[""]} 가 '성공' 으로
    #   통과해 모든 종목이 '기타 급등주' 로 떨어지던 문제 방지. 빈 결과는 실패(keep-previous).
    codes_in_uni = {s["코드"] for s in uni}
    real = [t for t in themes
            if isinstance(t, dict)
            and any(str(c).strip() in codes_in_uni for c in (t.get("codes") or []))]
    if not real:
        print(f"  [테마] 그룹핑 응답에 유효 종목이 없음(테마 {len(themes)}개, 껍데기) → 실패 처리")
        return None
    return real


def analyze_themes(market: dict) -> list:
    """
    1) 거래대금 상위(제외 후) 중 상승률 10%+ = 시드(대장주)
    2) 분석 대상 uni = 상승률 상위 20 ∪ 시드
    3) _llm_group_themes 로 '테마 묶기' 먼저(화면의 핵심 → 호출 예산 우선권)
    4) [증분] 아직 상세정보 없는 종목만 _llm_enrich → _stock_info_cache 에 축적
       (실패 종목은 _enrich_failed 에 기록해 30분간 재호출 안 함)
    5) 테마 내 상승률 내림차순, 테마는 총거래대금 순
    반환: [{"테마","요약","종목":[...],"_amount":..}, ...]

    ★ 그룹핑(구조)이 실패하면 폴백 단일그룹에 '_llm_failed':True 를 실어 반환한다.
      → main() 이 이를 감지해 '직전 정상 분류'를 유지(상승률 순서만 갱신)하고
        다음 스캔에서 재시도한다. (테마가 한 곳에 몰리는 붕괴 방지)
    """
    risers = [s for s in market["rise"] if not is_excluded(s["종목명"])][:RISE_TOP_N]
    trans_top = [s for s in market["trans"] if not is_excluded(s["종목명"])][:TRANS_TOP_N]
    seeds = [s for s in trans_top if s["등락률"] >= SEED_MIN_RATE]
    seed_codes = {s["코드"] for s in seeds}

    # 분석 대상 = 상승률 상위 20 ∪ 시드 (코드 기준)
    universe = {}
    for s in risers + seeds:
        universe.setdefault(s["코드"], s)
    uni = list(universe.values())
    print(f"  [테마] 상승률상위 {len(risers)} + 시드 {len(seeds)}(거래대금상위 중 ≥{SEED_MIN_RATE}%) "
          f"→ 분석 {len(uni)}종목")
    if not uni:
        return []

    if not _gemini_ready():
        # LLM 없으면 종목별 단순 나열 1개 그룹 (키 미설정 = 안정 상태, 재시도 신호 아님)
        members = sorted(uni, key=lambda x: x["등락률"], reverse=True)
        return [{"테마": "급등 종목", "요약": "",
                 "종목": [{**s, "업종": "", "사업": "", "특이사항": [], "핵심재료": ""} for s in members],
                 "_amount": sum(s.get("거래대금", 0) for s in members)}]

    # 뉴스 수집 (캐시 공유: enrich·group·조건부합이 함께 사용 → 네이버 반복호출 감소)
    news_map = {s["코드"]: _news_cached(s["코드"], ttl=170, n=6) for s in uni}
    empty = sum(1 for v in news_map.values() if not v)
    if empty:
        # 대부분 비어 있으면 뉴스 수집 경로가 죽은 것(2026-09-18: PC 페이지 410). 여기서 바로 보이게.
        print(f"  [뉴스] {len(uni)}종목 중 뉴스 0건 {empty}종목"
              + ("  ← 수집 경로 점검 필요" if empty >= max(3, len(uni) // 2) else ""))

    # 3) 테마 묶기(작은 호출) 를 '먼저' — 화면의 핵심이라 호출 예산의 우선권을 준다.
    #    (2026-09-15 수정) 이전엔 enrich 가 먼저라, 한도가 빡빡해지면 enrich 가 429 를 맞고
    #    쿨다운을 걸어버려 그룹핑은 순서가 영영 오지 않았다 → 테마가 오전 분류에 얼어붙음.
    themes_raw = _llm_group_themes(uni, news_map, seed_codes)

    # 4) 증분 상세분석: 캐시에 없는 종목만. 실패해도 위의 그룹핑은 이미 확보돼 있다.
    #    실패한 종목은 _enrich_failed 에 기록해 30분간 재호출하지 않는다(한도 보호).
    _now = time.time()
    need = [s for s in uni
            if s["코드"] not in _stock_info_cache
            and _now - _enrich_failed.get(s["코드"], 0.0) > _ENRICH_RETRY_GAP]
    if need:
        print(f"  [테마] 신규 {len(need)}종목 상세분석(증분)")
        got = _llm_enrich(need, news_map)
        _stock_info_cache.update(got)
        for s in need:
            if s["코드"] not in got:
                _enrich_failed[s["코드"]] = _now
    llm_failed = themes_raw is None
    if llm_failed:
        print("  [테마] LLM 테마 묶음 실패 → 폴백(직전 분류 유지 신호)")
        themes_raw = [{"테마": "급등 종목", "요약": "", "codes": [s["코드"] for s in uni]}]

    def enrich(s):
        it = _stock_info_cache.get(s["코드"], {})
        flags = [str(x).strip() for x in (it.get("특이사항") or []) if str(x).strip()]
        cap = s.get("시총억", 0)
        if 0 < cap < 400 and "저시총" not in flags:
            flags.append("저시총")
        return {**s,
                "업종": (it.get("업종") or "").strip(),
                "사업": (it.get("사업") or "").strip(),
                "특이사항": flags,
                "핵심재료": (it.get("핵심재료") or "").strip()}

    # 테마 그룹 구성
    groups, used = [], set()
    for t in themes_raw:
        codes = [str(c).strip() for c in (t.get("codes") or [])]
        members = [enrich(universe[c]) for c in codes if c in universe and c not in used]
        for c in codes:
            used.add(c)
        if not members:
            continue
        _record_upper_ts(members)
        members.sort(key=_member_sort_key)   # 상한가는 먼저 간 순, 나머지는 상승률순
        groups.append({
            "테마": (t.get("테마") or "기타").strip(),
            "요약": _join_summary(t.get("요약")),
            "등급": _clip_grade(t.get("등급")),   # 재료 인지도 1~5성 (Gemini 판정)
            "종목": members,
            "_amount": sum(m.get("거래대금", 0) for m in members),   # 테마 총 거래대금
        })

    # 미분류 종목 → 기타 급등
    leftover = [enrich(s) for s in uni if s["코드"] not in used]
    if leftover:
        _record_upper_ts(leftover)
        leftover.sort(key=_member_sort_key)
        groups.append({"테마": "기타 급등주", "요약": "", "종목": leftover,
                       "_amount": sum(m.get("거래대금", 0) for m in leftover)})

    # 테마 정렬: 개별 등락/기타 등 catch-all 은 항상 맨 아래, 나머지는 총 거래대금 높은 순
    #   (개별 등락은 '공통 재료 없는 잡동사니'라 상위 테마로 보이면 안 됨 → 하위 고정)
    groups.sort(key=lambda g: (_match_skip_theme(g.get("테마", "")), -g.get("_amount", 0)))
    if llm_failed and groups:
        groups[0]["_llm_failed"] = True   # main 이 직전 분류 유지하도록 신호
    return groups


# ─── 카카오 ───────────────────────────────────────

def _kakao_payload(message: str) -> dict:
    return {
        "template_object": json.dumps({
            "object_type": "text",
            "text": message,
            "link": {
                "web_url": "https://finance.naver.com",
                "mobile_web_url": "https://m.finance.naver.com",
            },
        })
    }


def _kakao_post(token: str, message: str):
    return requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data=_kakao_payload(message),
        timeout=10,
    )


def _load_kakao_token() -> dict:
    if os.path.exists(KAKAO_TOKEN_FILE):
        try:
            with open(KAKAO_TOKEN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[카카오] 토큰 파일 읽기 오류: {e}")
    return {}


def _save_kakao_token(tok: dict):
    try:
        with open(KAKAO_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(tok, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[카카오] 토큰 파일 저장 오류: {e}")


def _refresh_kakao(tok: dict) -> dict:
    try:
        r = requests.post(
            "https://kauth.kakao.com/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": KAKAO_REST_API_KEY,
                "refresh_token": tok.get("refresh_token", ""),
            },
            timeout=10,
        )
        return r.json()
    except Exception as e:
        print(f"[카카오] 갱신 오류: {e}")
        return {}


def _load_friends() -> list:
    """kakao_friends.json — [{label, access_token, refresh_token}, ...]"""
    if os.path.exists(KAKAO_FRIENDS_FILE):
        try:
            with open(KAKAO_FRIENDS_FILE, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[카카오] 친구 파일 읽기 오류: {e}")
    return []


def _save_friends(friends: list):
    try:
        with open(KAKAO_FRIENDS_FILE, "w", encoding="utf-8") as f:
            json.dump(friends, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[카카오] 친구 파일 저장 오류: {e}")


def _send_account(acct: dict, message: str, label: str) -> bool:
    """acct(dict) 의 토큰으로 전송. 401 이면 refresh_token 으로 갱신 후 재시도(acct 갱신)."""
    if not acct.get("access_token"):
        return False
    res = _kakao_post(acct["access_token"], message)
    ok = res.status_code == 200 and res.json().get("result_code") == 0
    if not ok and res.status_code == 401:
        print(f"[카카오/{label}] 토큰 만료 — 자동 갱신 시도...")
        new = _refresh_kakao(acct)
        if new.get("access_token"):
            acct.update(new)
            res = _kakao_post(acct["access_token"], message)
            ok = res.status_code == 200 and res.json().get("result_code") == 0
        else:
            print(f"[카카오/{label}] 갱신 실패: {new}")
    print(f"[카카오/{label}] " + ("전송 완료 ✓" if ok else f"실패: {res.text[:120]}"))
    return ok


def _split_message(message: str, limit: int = 900) -> list:
    """카카오 길이 제한 대비 — 테마(빈 줄) 단위로 메시지를 여러 개로 분할."""
    if len(message) <= limit:
        return [message]
    chunks, cur = [], ""
    for b in message.split("\n\n"):
        if len(b) > limit:                      # 한 블록이 한도보다 크면 줄 단위로
            if cur:
                chunks.append(cur); cur = ""
            line_cur = ""
            for ln in b.split("\n"):
                if line_cur and len(line_cur) + len(ln) + 1 > limit:
                    chunks.append(line_cur); line_cur = ln
                else:
                    line_cur = ln if not line_cur else line_cur + "\n" + ln
            cur = line_cur
        elif len(cur) + len(b) + 2 > limit:
            if cur:
                chunks.append(cur)
            cur = b
        else:
            cur = b if not cur else cur + "\n\n" + b
    if cur:
        chunks.append(cur)
    n = len(chunks)
    return [f"({i+1}/{n})\n{c}" if n > 1 else c for i, c in enumerate(chunks)]


def send_kakao(message: str) -> bool:
    if not KAKAO_ENABLED:
        return False
    success = False
    chunks = _split_message(message)

    # 1) 본인 — kakao_token.json (만료 시 자동 갱신)
    tok = _load_kakao_token()
    if tok.get("access_token"):
        ok = True
        for ch in chunks:
            ok = _send_account(tok, ch, "본인") and ok
            time.sleep(0.3)
        _save_kakao_token(tok)
        success = success or ok
    else:
        print("[카카오] kakao_token.json 없음 → 'python kakao_token_setup.py' 먼저 실행하세요")

    # 2) 친구들 — kakao_friends.json (각자 자동 갱신)
    friends = _load_friends()
    if friends:
        for fr in friends:
            okf = True
            for ch in chunks:
                okf = _send_account(fr, ch, fr.get("label", "친구")) and okf
                time.sleep(0.3)
            success = success or okf
        _save_friends(friends)

    # 3) 레거시 하드코딩 토큰(있으면, 갱신 없음)
    for token in KAKAO_TOKENS:
        for ch in chunks:
            try:
                res = _kakao_post(token, ch)
                ok = res.status_code == 200 and res.json().get("result_code") == 0
                success = success or ok
                time.sleep(0.3)
            except Exception as e:
                print(f"[카카오/추가] 오류: {e}")

    return success


# ─── 메시지 포맷 ──────────────────────────────────

def fmt_upper(stocks: list, theme_map: dict) -> str:
    lines = [f"🚨 상한가 알림 ({len(stocks)}개)\n"]
    for i, s in enumerate(stocks, 1):
        sign = "+" if s["등락률"] >= 0 else ""
        base = f"{i}. {s['종목명']} {sign}{s['등락률']}% {s['종가']:,}원 [{s['시장']}]"
        info = theme_map.get(s["코드"])
        if info and (info.get("theme") or info.get("summary")):
            tag = (info.get("theme") or "").strip()
            summ = (info.get("summary") or "").strip()
            if tag:
                base += f" - [{tag}]"
            if summ:
                base += (" " if tag else " - ") + summ
        lines.append(base)
    return "\n".join(lines)


def fmt_theme(groups: list) -> str:
    """테마별 그룹 + 종목 카드 형식."""
    now = datetime.now().strftime("%H:%M")
    lines = [f"📊 마켓중심 테마 분석 ({now})\n"]
    for g in groups:
        lines.append(f"🔥 [{g['테마']}]")
        if g.get("요약"):
            for ln in g["요약"].split("\n"):
                ln = ln.strip()
                if ln:
                    lines.append(f"📰 {ln}")
        for i, s in enumerate(g["종목"], 1):
            sign = "+" if s["등락률"] >= 0 else ""
            prof = []
            if s.get("업종"):
                prof.append(s["업종"])
            sz = _siztext(s.get("시총억", 0))
            if sz:
                prof.append(sz)
            if s.get("사업"):
                prof.append(s["사업"])
            prof += s.get("특이사항", [])

            line = f"  {i}. {s['종목명']} ({sign}{s['등락률']}%)"
            if prof:
                line += " : " + ", ".join(prof)
            lines.append(line)
        lines.append("")
    return "\n".join(lines).strip()


# ─── 대시보드 데이터 저장 ─────────────────────────

_news_cache = {}   # 코드 → (조회시각, 헤드라인 리스트) : 후보 뉴스 반복조회 방지


def _news_cached(code: str, ttl: int = 300, n: int = 3) -> list:
    """뉴스 헤드라인을 캐시(매 스캔 네이버 반복호출 방지). 조건부합·테마분석이 공유.
    캐시된 개수가 요청 n 이상이면 재사용, 부족하면 다시 가져와 갱신."""
    now = time.time()
    hit = _news_cache.get(code)
    if hit and now - hit[0] < ttl and len(hit[1]) >= n:
        return hit[1]
    heads = get_news_headlines(code, n=max(n, 6))
    _news_cache[code] = (now, heads)
    return heads


# 조건부합에서 제외할 유사테마(개별/미분류 catch-all)
_MATCH_SKIP_THEMES = ("기타 급등주", "기타", "급등 종목")


def _match_skip_theme(name: str) -> bool:
    return (name in _MATCH_SKIP_THEMES) or ("개별" in name)


def _mega_ref(name: str) -> bool:
    """삼성전자/SK하이닉스 등 초대형 참조주(우선주 포함)는 '대금 형성'·대장 판정에서 제외."""
    if not name:
        return False
    base = re.sub(r"우[AB]?$", "", name).strip()   # 삼성전자우 → 삼성전자
    return is_excluded(name) or is_excluded(base)


# ── 종목별 최초 상한가 진입 시각 추적 (조건부합 대표 선정용) ──
#   같은 테마에 상한가가 여러 개면 '가장 먼저 상한가를 간' 종목을 대표로 띄우기 위함.
#   오늘자만 유지(날짜 바뀌면 리셋). --once 서브프로세스/재시작에도 살아남도록 파일에 보존.
_upper_ts_cache = None   # {code: "HH:MM:SS"} — 오늘자 최초 상한가 진입 시각


def _load_upper_ts() -> dict:
    """오늘자 최초 상한가 진입 시각 맵을 로드. 날짜가 다르면 빈 맵."""
    global _upper_ts_cache
    if _upper_ts_cache is not None:
        return _upper_ts_cache
    today = datetime.now().strftime("%Y-%m-%d")
    _upper_ts_cache = {}
    try:
        with open(UPPER_TS_FILE, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("date") == today:
            _upper_ts_cache = dict(obj.get("ts", {}))
    except Exception:
        pass
    return _upper_ts_cache


def _member_sort_key(m, ts=None):
    """테마 내 종목 정렬 키.
    상한가 종목: '먼저 상한가 간' 순(진입시각 오름차순, 미기록은 뒤) →
    그 외: 상승률 높은 순.  예) 시초가 상한가(09:00 기록)가 늦은 상한가보다 위."""
    if ts is None:
        ts = _load_upper_ts()
    rate = m.get("등락률", 0) or 0
    if rate >= UPPER_LIMIT_THRESHOLD:
        return (0, ts.get(m.get("코드", ""), "99:99:99"), -rate)
    return (1, "", -rate)


def _record_upper_ts(members: list) -> dict:
    """members 중 상한가(≥THRESHOLD) 종목의 '최초' 진입 시각을 기록(이미 있으면 유지).
    변경이 있으면 파일에 저장. 반환값은 오늘자 {code: 'HH:MM:SS'} 맵."""
    ts = _load_upper_ts()
    now = datetime.now()
    now_hms = now.strftime("%H:%M:%S")
    changed = False
    for m in members:
        code = m.get("코드", "")
        if not code:
            continue
        if (m.get("등락률", 0) or 0) >= UPPER_LIMIT_THRESHOLD and code not in ts:
            ts[code] = now_hms          # 처음 상한가로 관측된 시각만 기록
            changed = True
    if changed:
        try:
            tmp = UPPER_TS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"date": now.strftime("%Y-%m-%d"), "ts": ts},
                          f, ensure_ascii=False, indent=2)
            os.replace(tmp, UPPER_TS_FILE)
        except Exception:
            pass
    return ts


def compute_match(groups: list) -> list:
    """조건부합(대시보드 최상단). 두 가지 경로로 대표 종목을 뽑는다.
    ① 테마 경로: 같은 테마에 동조주(2종목 이상)가 있고 그중 '누군가' 대금을 형성하면
       (대금 ≥ MATCH_AMOUNT_MIN_EOK) 그 테마의 대표 종목 표시. 대표 = 상한가가 여럿이면
       '가장 먼저 상한가 간' 종목, 아니면 상승률 1위(대장). 대장 본인 대금은 낮아도 됨.
    ② 단독 급등 경로: 동조주 없이 혼자 크게 급등한 종목(상승률 ≥ MATCH_SOLO_RATE_MIN)은
       **대금 ≥ MATCH_AMOUNT_HL_EOK(3천억)** 일 때만 표시(단독은 검증이 약하므로 대금 문턱↑).
    초대형 참조주(삼성전자(우) 등)는 제외. 상승률순 상위 MATCH_MAX개.
    빨간 테두리 = 대금 형성 종목의 대금 ≥ HL(단독 급등은 항상 강조). 뉴스는 소프트(링크만)."""
    min_won = MATCH_AMOUNT_MIN_EOK * 1e8
    hl_won = MATCH_AMOUNT_HL_EOK * 1e8
    # 이번 스캔의 모든 상한가 종목 진입 시각 기록/로드
    upper_ts = _record_upper_ts([m for g in groups for m in g.get("종목", [])])
    cands = []
    for g in groups:
        theme = g.get("테마", "")
        eff = [m for m in g.get("종목", []) if not _mega_ref(m.get("종목명", ""))]
        if not eff:
            continue

        # ── ① 테마 경로: 동조주 2종목↑ + 대금 형성(1000억↑) ──
        if not _match_skip_theme(theme) and len(eff) >= 2:
            # 대표 = 테마의 '첫번째 종목'과 동일 기준(_member_sort_key):
            # 상한가가 있으면 가장 먼저 상한가 간 종목, 없으면 상승률 1위.
            leader = min(eff, key=lambda m: _member_sort_key(m, upper_ts))
            former = max(eff, key=lambda m: m.get("거래대금", 0) or 0)  # 대금 형성 종목
            amt_won = former.get("거래대금", 0) or 0
            co = [m for m in eff if m is not leader and m.get("등락률", 0) > 0]
            if amt_won >= min_won and co:          # 대금 형성 + 동조주 있음
                cands.append({
                    "name": leader["종목명"], "code": leader.get("코드", ""),
                    "rate": leader.get("등락률", 0),
                    "theme": theme, "sector": leader.get("업종", ""),
                    "cap": leader.get("시총억", 0),
                    "highlight": amt_won >= hl_won,        # 대금 형성 종목 3천억↑ → 빨간 테두리
                    "peers": len(co),
                    "amt_name": former.get("종목명", ""),   # 대금 형성 종목명(대장과 다를 수 있음)
                    "amt_value": round(amt_won / 1e8),     # 그 대금(억)
                })
                continue   # 이 테마는 대표 1종목으로 처리 완료

        # ── ② 단독 급등 경로: 혼자 크게 급등(상승률↑) + 대금 3천억↑ 만 ──
        #    (개별 등락/기타/1종목 테마, 또는 ①에서 동조·대금 조건 미달한 테마)
        for m in eff:
            amt = m.get("거래대금", 0) or 0
            rate = m.get("등락률", 0) or 0
            if amt >= hl_won and rate >= MATCH_SOLO_RATE_MIN:
                cands.append({
                    "name": m["종목명"], "code": m.get("코드", ""),
                    "rate": rate,
                    "theme": theme, "sector": m.get("업종", ""),
                    "cap": m.get("시총억", 0),
                    "highlight": True,             # 대금 3천억↑ → 항상 강조
                    "peers": 0,                    # 동조주 없음(단독)
                    "amt_name": m.get("종목명", ""),
                    "amt_value": round(amt / 1e8),
                })

    # 코드 중복 제거(방어) 후 '테마 순위순'(groups 정렬 = 총 거래대금순, catch-all 뒤)
    # 상위 MATCH_MAX개 — 최상단 카드 = 가장 상위 테마의 첫번째 종목 (사용자 결정)
    seen, uniq = set(), []
    for c in cands:
        if c["code"] in seen:
            continue
        seen.add(c["code"])
        uniq.append(c)
    cands = uniq[:MATCH_MAX]
    for c in cands:                               # 후보에만 뉴스 부착(≤3종목)
        heads = _news_cached(c["code"])
        c["news"] = bool(heads)
        c["news_url"] = f"https://finance.naver.com/item/news_news.naver?code={c['code']}"
    return cands


# ─── 전일 연속 상한가(N상) 추적 ───────────────────────────
#   매일 20:00 cron(`--close-scan`)이 그날 '마감 상한가' 종목을 수집해 연속 상한가 일수를
#   누적 저장한다. 장중 봇은 이 파일을 읽어 각 종목에 '전일 N상'을 표기한다.
#   예) 어제 처음 상한가 → '전일 1상', 어제까지 2연속 상한가 → '전일 2상'.
UPPER_STREAK_FILE = os.path.join(_BASE_DIR, "upper_streak.json")

# 2026 공휴일(연속 상한가 영업일 판정용) — krx_alert.py 와 동일 목록
# 거래일 달력은 market_calendar 로 통일(공휴일 표 중복 제거).
# _prev_bday 는 기존과 동일하게 '직전 거래일'(오늘 제외) 의미 → prev_bday 매핑.
from market_calendar import HOLIDAYS as _HOLIDAYS_2026, is_bday as _is_bday, prev_bday as _prev_bday  # noqa: E402


def _read_streak_file() -> dict:
    try:
        with open(UPPER_STREAK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_streak_file(obj: dict):
    try:
        tmp = UPPER_STREAK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, UPPER_STREAK_FILE)
    except Exception as e:
        print(f"  [상한가연속] 저장 오류: {e}")


def _prev_upper_streaks() -> dict:
    """전일(직전 영업일) 마감 기준 연속 상한가 맵 {code: N}. 조건 안 맞으면 {}.
    파일 날짜가 '오늘 기준 직전 영업일'과 정확히 일치할 때만 사용(오늘자/오래된 데이터 배제)."""
    obj = _read_streak_file()
    d = obj.get("date")
    if not d:
        return {}
    try:
        fdate = datetime.strptime(d, "%Y-%m-%d").date()
    except Exception:
        return {}
    if fdate == _prev_bday(datetime.now().date()):
        return obj.get("streak", {}) or {}
    return {}


def update_upper_streak():
    """[20:00 cron / `--close-scan`] 오늘 '마감 상한가' 종목을 수집해 연속 상한가 일수 누적 저장.
    - 오늘 상한가(≥UPPER_LIMIT_THRESHOLD) 종목 집합을 구한다(장 마감 후 시세 = 종가 기준).
    - 직전 저장이 '직전 영업일'이면 연속으로 인정(+1), 아니면 1부터 새로 센다.
    - 비영업일(주말/공휴일)에는 스킵(종가 데이터가 그대로라 중복 카운트 방지)."""
    today = datetime.now().date()
    if not _is_bday(today):
        print(f"[상한가연속] {today} 비영업일 — 스킵")
        return
    # 방어선: 공휴일 표가 틀려도(예: 제헌절 누락) 실제 거래일 달력으로 재확인.
    # 장이 닫힌 날엔 네이버가 직전 종가를 그대로 줘서 '유령 거래일'로 중복 카운트되는 것을 막는다.
    try:
        import price_source
        tdays = price_source.fetch_trading_days(10)
        if tdays and today.isoformat() not in tdays:
            print(f"[상한가연속] {today} 실제 거래일 아님(달력 확인) — 스킵")
            return
    except Exception as e:
        print("[상한가연속] 거래일 달력 확인 실패(공휴일표로 진행):", str(e)[:120])
    print("[상한가연속] 마감 시세 수집 중...", flush=True)
    market = fetch_market_data()
    if not market["all"]:
        print("[상한가연속] 데이터 없음 — 스킵")
        return
    today_upper = {code for code, s in market["all"].items()
                   if (s.get("등락률", 0) or 0) >= UPPER_LIMIT_THRESHOLD}
    prev = _read_streak_file()
    prev_streak = {}
    pd = prev.get("date")
    if pd:
        try:
            if datetime.strptime(pd, "%Y-%m-%d").date() == _prev_bday(today):
                prev_streak = prev.get("streak", {}) or {}   # 직전 영업일 → 연속 인정
        except Exception:
            pass
    new_streak = {code: prev_streak.get(code, 0) + 1 for code in today_upper}
    _write_streak_file({"date": today.isoformat(), "streak": new_streak})
    top = sorted(new_streak.items(), key=lambda x: -x[1])[:5]
    print(f"[상한가연속] {today} 마감 상한가 {len(today_upper)}종목 저장 (최다연속 상위: {top})")


# 조건부합 마지막 표시 후보(고정용): 새 후보가 나오기 전까진 직전 후보를 그대로 유지
_last_match = []


def save_dashboard_data(upper: list, theme_map: dict, groups: list):
    """웹 대시보드가 읽을 최신 결과를 data.json 으로 저장."""
    global _last_match
    streak = _prev_upper_streaks()   # {code: 전일까지 연속 상한가 일수} — '전일 N상' 표기용
    new_match = compute_match(groups)
    if new_match:
        _last_match = new_match          # 새 후보 등장 → 교체 (= '다른 종목이 나올 때')
    else:
        # 고정: 새 후보가 없으면 직전 후보를 유지(사라지지 않게), 현재 시세만 갱신
        price_map = {m.get("코드"): m for g in groups for m in g.get("종목", [])}
        for c in _last_match:
            fr = price_map.get(c.get("code"))
            if fr:
                c["rate"] = fr.get("등락률", c.get("rate"))
                if fr.get("거래대금"):
                    c["amt_value"] = round((fr.get("거래대금") or 0) / 1e8)
    for c in _last_match:
        c["psang"] = streak.get(c.get("code", ""), 0)   # 전일 연속 상한가 일수
    data = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "indices": _get_indices_cached(),   # 뉴스 탭 상단 지수 카드(코스피/코스닥/나스닥/S&P/원달러)
        "match": _last_match,
        "upper": [{
            "name": s["종목명"], "rate": s["등락률"], "market": s["시장"],
            "theme": (theme_map.get(s["코드"]) or {}).get("theme", ""),
            "amount": round(s.get("거래대금", 0) / 1e8),      # 거래대금(억원)
            "psang": streak.get(s["코드"], 0),               # 전일 연속 상한가 일수
        } for s in upper],
        "themes": [{
            "theme": g["테마"], "summary": g.get("요약", ""),
            "grade": _clip_grade(g.get("등급")),   # 재료 인지도 1~5성
            "amount": round(sum(m.get("거래대금", 0) for m in g["종목"]) / 1e8),  # 테마 총 거래대금(억원)
            "stocks": [{
                "name": m["종목명"], "rate": m["등락률"],
                "code": m.get("코드", ""),                    # 종목뉴스 링크용
                "sector": m.get("업종", ""), "cap": m.get("시총억", 0),
                "flags": m.get("특이사항", []),
                "amount": round(m.get("거래대금", 0) / 1e8),   # 거래대금(억원)
                "psang": streak.get(m.get("코드", ""), 0),     # 전일 연속 상한가 일수
            } for m in g["종목"]],
        } for g in groups],
    }
    try:
        # 원자적 저장(tmp→replace): 봇 루프와 --once 서브프로세스가 동시에 써도 부분읽기 방지
        tmp = DATA_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, DATA_FILE)
        print(f"  [data] 저장: 상한가 {len(data['upper'])}, 테마 {len(data['themes'])}")
    except Exception as e:
        print(f"  [data] 저장 오류: {e}")


def refresh_group_prices(groups: list, market: dict) -> list:
    """LLM 호출 없이 기존 테마 그룹 종목의 시세(등락률/종가/거래대금/시총)만 최신값으로 갱신.
    상위 목록에서 빠진 종목은 직전 값 유지(다음 LLM 재분류 때 정리됨)."""
    allmap = market.get("all", {})
    for g in groups:
        for m in g.get("종목", []):
            fresh = allmap.get(m.get("코드"))
            if not fresh:
                continue
            m["등락률"] = fresh.get("등락률", m.get("등락률", 0))
            if fresh.get("종가"):
                m["종가"] = fresh["종가"]
            if fresh.get("거래대금"):
                m["거래대금"] = fresh["거래대금"]
            if fresh.get("시총억"):
                m["시총억"] = fresh["시총억"]
        # 새로 상한가 도달한 종목의 진입시각을 이번 스캔에서 기록한 뒤 정렬
        _record_upper_ts(g.get("종목", []))
        g.get("종목", []).sort(key=_member_sort_key)
    return groups


def _touch(path: str):
    """생존/신호용 파일 타임스탬프 기록."""
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


# ─── 장 시간 체크 ─────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return MARKET_PRE_MIN <= t <= MARKET_CLOSE_MIN


def is_pre_market() -> bool:
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return MARKET_PRE_MIN <= t < MARKET_OPEN_MIN


# ─── 메인 루프 ────────────────────────────────────

def run_once():
    """장 시간 무시하고 현재(=장 마감 후엔 오늘 종가) 데이터로 1회 실행 후 종료."""
    print("=" * 50)
    print("  단발 실행 (오늘 종가 기준) — 장 시간 무시")
    print(f"  Gemini LLM: {'연동됨 ✓' if _gemini_ready() else '미설정 (키 입력 필요) ✗'}")
    print("=" * 50)

    print("① 네이버 시세 수집 중...", flush=True)
    market = fetch_market_data()
    if not market["all"]:
        print("  데이터 없음 — 종료")
        return
    print(f"   수집 완료: 총 {len(market['all'])}종목 "
          f"(거래대금 {len(market['trans'])}, 상승률 {len(market['rise'])})", flush=True)

    stocks = list(market["all"].values())

    # ① 상한가 알림 (+ 테마/뉴스 요약) — 먼저 상한가 간 순
    upper = [s for s in stocks if s["등락률"] >= UPPER_LIMIT_THRESHOLD]
    _record_upper_ts(upper)
    upper.sort(key=_member_sort_key)
    theme_map = {}
    if upper:
        print(f"② 상한가 {len(upper)}종목 → 뉴스/테마 분석 중...", flush=True)
        theme_map = classify_upper(upper)
        msg = fmt_upper(upper, theme_map)
        print("\n" + msg, flush=True)
        send_kakao(msg)
    else:
        print("② 상한가 종목 없음", flush=True)

    # ② 티마식 테마 분석
    print("③ 테마 분석 중 (시드 선별 → 뉴스 검색 → LLM 분류)...", flush=True)
    themes = analyze_themes(market)
    if themes:
        msg = fmt_theme(themes)
        print("\n" + msg)
        send_kakao(msg)
    else:
        print("  테마 조건 충족 종목 없음")

    save_dashboard_data(upper, theme_map, themes or [])
    try:
        if os.path.exists(REFRESH_LOCK):
            os.remove(REFRESH_LOCK)   # 웹 새로고침(장외) 락 해제 → 다음 새로고침 즉시 가능
    except OSError:
        pass
    print("\n단발 실행 완료.")


def main():
    print("=" * 50)
    print("  주식 모니터링 봇 시작 (티마식 테마 분석)")
    print(f"  상한가 기준: {UPPER_LIMIT_THRESHOLD}%  |  시세주기: {SCAN_INTERVAL}s  |  테마LLM주기: {THEME_LLM_INTERVAL//60}분")
    print(f"  Gemini LLM: {'연동됨 ✓' if _gemini_ready() else '미설정 (키 입력 필요) ✗'}")
    print("=" * 50)

    alerted = set()
    upper_theme_map = {}
    last_themes = []
    last_theme_ts = 0.0
    first_run = True

    while True:
        now = datetime.now()
        t = now.hour * 60 + now.minute

        # 주말 → 종료
        if now.weekday() >= 5:
            print(f"[{now.strftime('%H:%M')}] 주말 — 봇 종료")
            break
        # 09:00 이전 → 개장 대기
        if t < MARKET_OPEN_MIN:
            print(f"[{now.strftime('%H:%M')}] 개장 전 — 대기...")
            time.sleep(30)
            continue
        # 마감 이후 → 오늘 세션 종료 (cron 이 다음 거래일 09:00 에 재시작)
        if t > MARKET_CLOSE_MIN:
            print(f"[{now.strftime('%H:%M')}] 장 마감({MARKET_CLOSE_MIN // 60:02d}:{MARKET_CLOSE_MIN % 60:02d}) — 오늘 세션 종료")
            break

        _touch(HEARTBEAT_FILE)   # 웹서버가 '봇 가동 중' 확인용(수동 새로고침 라우팅에 사용)

        # 웹 새로고침 버튼 신호 — 있으면 상한가+테마를 raw 부터 강제 전체 재분석
        force_refresh = os.path.exists(REFRESH_FLAG)
        if force_refresh:
            print(f"[{now.strftime('%H:%M')}] 🔄 수동 새로고침 요청 — 상한가+테마 전체 재분석")
            try:
                os.remove(REFRESH_FLAG)
            except OSError:
                pass

        try:
            print(f"\n[{now.strftime('%H:%M')}] 스캔 중...")
            market = fetch_market_data()
            if not market["all"]:
                print("  데이터 없음 — 재시도")
                time.sleep(15)
                continue

            stocks = list(market["all"].values())

            # ① 상한가 — 매 스캔 최신 목록(등락률 즉시 반영). 신규는 카톡, 수동새로고침 시 전체 재분류
            #    정렬: 먼저 상한가 간 순(진입시각 파일 기록 기반)
            upper_all = [s for s in stocks if s["등락률"] >= UPPER_LIMIT_THRESHOLD]
            _record_upper_ts(upper_all)
            upper_all.sort(key=_member_sort_key)
            new_upper = [s for s in upper_all if s["코드"] not in alerted]
            classify_targets = upper_all if force_refresh else new_upper
            if classify_targets:
                tm = classify_upper(classify_targets)
                upper_theme_map.update(tm)
            if new_upper:
                for s in new_upper:
                    alerted.add(s["코드"])          # 분석 완료 표시(중복 카톡 방지)
                msg = fmt_upper(new_upper, upper_theme_map)
                print(msg)
                send_kakao(msg)                      # KAKAO_ENABLED=False 면 내부에서 전송 skip
            else:
                print("  상한가 신규 없음")

            # ② 테마 — THEME_LLM_INTERVAL 마다(또는 첫실행/수동새로고침) LLM 재분류, 그 사이엔 시세만 갱신
            now_ts = time.time()
            do_theme_llm = first_run or force_refresh or (now_ts - last_theme_ts >= THEME_LLM_INTERVAL)
            if do_theme_llm:
                was_first = first_run
                groups = analyze_themes(market)
                llm_failed = bool(groups) and any(g.get("_llm_failed") for g in groups)
                if groups and not llm_failed:
                    # 정상 분류 성공 → 채택
                    last_themes = groups
                    last_theme_ts = now_ts
                    first_run = False
                    msg = fmt_theme(groups)
                    print(msg)
                    if was_first or force_refresh:
                        send_kakao(msg)              # 카톡 테마 알림은 첫실행/수동새로고침 때만(도배 방지)
                elif last_themes:
                    # LLM 실패 & 직전 정상 분류 있음 → 그대로 유지, 상승률 순서만 갱신 후 다음 스캔 재시도
                    #   (last_theme_ts 를 갱신하지 않으므로 다음 스캔에서 즉시 재분류 시도)
                    refresh_group_prices(last_themes, market)
                    print("  [테마] LLM 실패 — 직전 분류 유지(상승률 순서만 갱신), 다음 스캔 재시도")
                else:
                    # 첫 시도부터 실패 & 직전 분류 없음 → 폴백이라도 임시 표시하되 곧 재시도
                    if groups:
                        last_themes = groups
                        print(fmt_theme(groups))
                    print("  [테마] LLM 실패 — 임시 표시, 다음 스캔 재시도")
            else:
                refresh_group_prices(last_themes, market)   # LLM 없이 시세만 최신화(상승률 순 재정렬)
                remain = int(THEME_LLM_INTERVAL - (now_ts - last_theme_ts))
                print(f"  테마 시세만 갱신 (다음 LLM 재분류까지 {max(0, remain)}s)")

            # 대시보드 데이터 저장 (매 스캔)
            save_dashboard_data(upper_all, upper_theme_map, last_themes)
        except Exception:
            # 스캔 한 번의 실패(LLM 응답 형태 이상·네트워크 등)가 봇 전체를 죽이면 안 된다.
            # (2026-09-18) gemma 가 items 를 배열로 돌려줘 .get 에서 터지며 봇이 종료된 사고.
            #   → 이번 스캔만 건너뛰고 30초 뒤 계속. 대시보드는 직전 data.json 을 유지한다.
            print("  [오류] 스캔 중 예외 — 이번 스캔 건너뜀\n" + traceback.format_exc())
            time.sleep(30)
            continue

        print(f"  → {SCAN_INTERVAL}s 후 재스캔")
        time.sleep(SCAN_INTERVAL)


# ─── 셀프 테스트 (네트워크/LLM 없이 포맷·로직 점검) ──

def _selftest():
    print("=== SELFTEST: 포맷 & 로직 점검 (네트워크/LLM 미사용) ===\n")
    mock = {
        "trans": [
            {"코드": "000001", "종목명": "다스코", "시장": "KOSPI", "종가": 4465, "등락률": 29.99, "거래대금": 5_000_00000000, "순위": 1},
            {"코드": "000002", "종목명": "삼성전자", "시장": "KOSPI", "종가": 70000, "등락률": 1.2, "거래대금": 9_000_00000000, "순위": 2},
            {"코드": "000003", "종목명": "현대로템", "시장": "KOSPI", "종가": 50000, "등락률": 12.5, "거래대금": 3_000_00000000, "순위": 3},
        ],
        "rise": [
            {"코드": "000001", "종목명": "다스코", "시장": "KOSPI", "종가": 4465, "등락률": 29.99, "거래대금": 5_000_00000000, "순위": 1},
            {"코드": "000004", "종목명": "비츠로테크", "시장": "KOSDAQ", "종가": 12000, "등락률": 18.0, "거래대금": 800_00000000, "순위": 2},
            {"코드": "000003", "종목명": "현대로템", "시장": "KOSPI", "종가": 50000, "등락률": 12.5, "거래대금": 3_000_00000000, "순위": 3},
        ],
        "all": {},
    }
    for s in mock["trans"] + mock["rise"]:
        s.setdefault("시총억", 800)
    mock["all"] = {s["코드"]: s for s in mock["trans"] + mock["rise"]}

    # 제외 필터 점검
    assert is_excluded("삼성전자") and is_excluded("KODEX 200") and not is_excluded("다스코")
    assert _siztext(2100) == "시총2100억대" and _siztext(679) == "시총600억대" and _siztext(12000) == "시총1.2조"
    print("[OK] 제외 필터 + 시총 표기 정상")

    # 테마 그룹 + 카드 포맷 (LLM 결과를 모의로 주입)
    sample = [
        {"테마": "호남 반도체 클러스터(지역)",
         "요약": "정부, 800조 규모 호남권 반도체 클러스터 청사진 제시\n호남 연고·토지 보유 기업 일제히 급등",
         "종목": [
            {"종목명": "다스코", "등락률": 29.99, "시총억": 800, "업종": "건설관련주",
             "사업": "도로안전시설물사업", "특이사항": [],
             "핵심재료": "전남 장흥 7000억 규모 태양광 건설·운영권 확보"},
            {"종목명": "남화토건", "등락률": 29.94, "시총억": 1200, "업종": "건설관련주",
             "사업": "토목·건축사업", "특이사항": ["투자주의종목"],
             "핵심재료": "본사 전남 화순 소재 — 호남 개발 수혜 기대"},
         ]},
        {"테마": "신규상장",
         "요약": "코스닥 신규상장주 첫날 수급 쏠림\n -",
         "종목": [
            {"종목명": "져스텍", "등락률": 40.24, "시총억": 2100, "업종": "기계관련주",
             "사업": "모션기술시스템사업", "특이사항": ["금일 신규상장"],
             "핵심재료": "금일 코스닥 신규상장 첫날 급등"},
         ]},
    ]
    print("\n--- fmt_theme(테마 그룹) 출력 예시 ---")
    print(fmt_theme(sample))

    # 상한가 포맷
    upper = [s for s in mock["trans"] if s["등락률"] >= UPPER_LIMIT_THRESHOLD]
    tmap = {"000001": {"theme": "철강·태양광", "summary": "구조용 강관 수요 기대 + 정책 수혜 부각"}}
    print("\n--- fmt_upper 출력 예시 ---")
    print(fmt_upper(upper, tmap))
    print("\n=== SELFTEST 완료 ===")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--once" in sys.argv:
        run_once()
    elif "--close-scan" in sys.argv:
        update_upper_streak()      # 매일 20:00 cron: 마감 상한가 → 연속 상한가 일수 누적
    else:
        main()
