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
import re
import sys
import requests
from datetime import datetime, timedelta
from collections import defaultdict

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
KAKAO_TOKENS = [
    # "친구_access_token_여기에",  # (레거시) 자동갱신 안 됨. 친구는 kakao_add_friend.py 사용 권장
]

# 카카오 알림 사용 여부 — False 면 카톡 전송 안 함(대시보드만 사용). 다시 켜려면 True.
KAKAO_ENABLED = False

# ── Gemini (Google AI Studio) API 키 ────────────────
GEMINI_API_KEY = _cfg("GEMINI_API_KEY")
GEMINI_MODEL = "gemini-2.5-flash"   # 무료 티어 확인됨 ✓ (대안: "gemini-2.5-flash-lite")

# 상한가 알림 기준 등락률 (%)
UPPER_LIMIT_THRESHOLD = 29.0

# ── 티마식 테마 분석 파라미터 ───────────────────────
TRANS_TOP_N   = 15    # 거래대금 상위 N개(대장주 후보) — 삼성전자/SK하이닉스/ETF/ETN 제외 후
SEED_MIN_RATE = 10.0  # 시드 종목 최소 상승률 (%)
RISE_TOP_N    = 20    # 상승률 상위 N개 (동일 테마 편입 대상)

# 스캔 주기 (초)  →  300 = 5분
SCAN_INTERVAL = 300

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


def fetch_market_data() -> dict:
    """
    네이버 금융에서 데이터 수집.
    반환: {
      "trans": [...],   # 거래대금 상위 (KOSPI+KOSDAQ 합쳐 거래대금 내림차순)
      "rise":  [...],   # 상승률 상위 (KOSPI+KOSDAQ 합쳐 등락률 내림차순)
      "all":   {code: stock}  # 코드별 통합 (상한가 탐지용)
    }
    """
    quant, rise = [], []

    for sosok, market in [("0", "KOSPI"), ("1", "KOSDAQ")]:
        # 거래대금/거래량 페이지 (거래대금 정확값 확보) — 2페이지까지
        for page in (1, 2):
            quant += _parse_quant_page(
                f"https://finance.naver.com/sise/sise_quant.naver?sosok={sosok}&page={page}",
                market
            )
        # 상승률 상위 (2페이지까지) — 동일 파서로 시총/거래대금까지 확보
        for page in (1, 2):
            rise += _parse_quant_page(
                f"https://finance.naver.com/sise/sise_rise.naver?sosok={sosok}&page={page}",
                market
            )

    # 거래대금 종목 코드별 통합(최대값 유지)
    qmap = {}
    for s in quant:
        if s["코드"] not in qmap or s["거래대금"] > qmap[s["코드"]]["거래대금"]:
            qmap[s["코드"]] = s

    # 상승률 종목에 거래대금/시총 보강(quant 에 있으면)
    for s in rise:
        q = qmap.get(s["코드"])
        if q:
            if not s.get("거래대금"):
                s["거래대금"] = q["거래대금"]
            if not s.get("시총억"):
                s["시총억"] = q["시총억"]

    # 코드별 통합 (상한가 탐지용) — quant 우선
    seen = {}
    for s in list(qmap.values()) + rise:
        seen.setdefault(s["코드"], s)

    def _dedup(lst):
        out, seen_c = [], set()
        for s in lst:
            if s["코드"] not in seen_c:
                out.append(s); seen_c.add(s["코드"])
        return out

    return {
        "trans": sorted(qmap.values(), key=lambda x: x["거래대금"], reverse=True),
        "rise":  _dedup(sorted([s for s in rise if s["등락률"] > 0],
                               key=lambda x: x["등락률"], reverse=True)),
        "all":   seen,
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
    """종목코드로 네이버 금융 종목뉴스 페이지에서 최신 제목 n개 반환."""
    try:
        url = f"https://finance.naver.com/item/news_news.naver?code={code}&page=1"
        res = requests.get(url, headers=NAVER_HEADERS, timeout=10)
        res.encoding = 'euc-kr'
        html = res.text
        titles = re.findall(r'<a[^>]+class="tit"[^>]*>(.*?)</a>', html, re.S)
        if not titles:
            titles = re.findall(r'class="tit"[^>]*>(.*?)</a>', html, re.S)
        out, seen = [], set()
        for t in titles:
            title = _clean_html(t)
            if title and title not in seen:
                seen.add(title)
                out.append(title[:120])
            if len(out) >= n:
                break
        return out
    except Exception as e:
        print(f"  [뉴스] {code} 조회 오류: {e}")
        return []


# ─── Gemini LLM ───────────────────────────────────

def _gemini_ready() -> bool:
    return bool(GEMINI_API_KEY) and "붙여넣기" not in GEMINI_API_KEY


def gemini_json(prompt: str, retries: int = 2):
    """Gemini 호출 → JSON 파싱해서 dict/list 반환. 실패 시 None."""
    if not _gemini_ready():
        return None
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}")
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},   # 2.5-flash 사고모드 OFF → 속도↑
        },
    }
    for attempt in range(retries + 1):
        try:
            r = requests.post(url, json=body, timeout=120)
            if r.status_code == 200:
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(txt)
            elif r.status_code == 429:
                print("  [Gemini] 호출 한도(429) — 20초 대기 후 재시도")
                time.sleep(20)
            else:
                print(f"  [Gemini] HTTP {r.status_code}: {r.text[:200]}")
                time.sleep(3)
        except Exception as e:
            print(f"  [Gemini] 오류: {e}")
            time.sleep(3)
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
    if data and isinstance(data.get("items"), list):
        for it in data["items"]:
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


def analyze_themes(market: dict) -> list:
    """
    원래 6단계:
      1) 거래대금 상위 15(제외 후) 중 상승률 10%+ = 시드(대장주)
      2) 시드 뉴스로 테마 분류
      3) 상승률 상위 20 중 동일 테마 편입
      4) 테마 내 상승률 내림차순, 테마는 시드 포함/최고상승률 우선
    반환: [{"테마": str, "요약": str(2줄), "종목": [stock(+업종/시총/사업/특이사항/핵심재료), ...]}, ...]
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
        # LLM 없으면 종목별 단순 나열 1개 그룹
        members = sorted(uni, key=lambda x: x["등락률"], reverse=True)
        return [{"테마": "급등 종목", "요약": "",
                 "종목": [{**s, "업종": "", "사업": "", "특이사항": [], "핵심재료": ""} for s in members]}]

    # 뉴스 수집 (종목코드 기반)
    news_map = {}
    for s in uni:
        news_map[s["코드"]] = get_news_headlines(s["코드"], n=8)
        time.sleep(0.35)

    blocks = []
    for s in uni:
        tag = " [거래대금대장주]" if s["코드"] in seed_codes else ""
        nlines = "\n    ".join("· " + h for h in news_map[s["코드"]]) or "· (관련 뉴스 없음)"
        blocks.append(
            f'[{s["코드"]}] {s["종목명"]} (+{s["등락률"]}%, 시총 {s.get("시총억", 0)}억){tag}\n    {nlines}'
        )

    prompt = (
        "너는 한국 증시 테마 분석가다. 아래 급등 종목들의 최신 뉴스를 보고 "
        "(1) 종목별 정보를 정리하고 (2) 같은 재료/테마끼리 묶어라. 뉴스에 없는 사실은 지어내지 마라.\n\n"
        "[items] 각 종목:\n"
        "- code: 종목코드\n"
        "- 업종: 'OO관련주' (예: 기계관련주, 화장품관련주, 이차전지관련주, 건설관련주, 레저관련주)\n"
        "- 사업: 핵심 사업 명사구 (예: 도로안전시설물사업, 골프장사업)\n"
        "- 특이사항: 뉴스에 명시된 것만 배열 (신규상장/투자주의종목/투자환기종목/관리종목/자금조달 진행 중/"
        "CB발행 결정/유상증자 결정/최대주주 변경/공개매수/상장폐지 추진 등). 없으면 [].\n"
        "- 핵심재료: 상승 핵심 재료를 구체적 수치/계약/정책을 살려 1~2문장. 뚜렷한 재료 없으면 '개별 등락'.\n\n"
        "[themes] 테마 묶음:\n"
        "- 테마: 시장 통용 테마명 (예: 원전, 이차전지, 로봇, 호남 반도체 클러스터(지역), 신규상장 등)\n"
        "- 요약: 그 테마가 오늘 부각된 핵심 뉴스를 2줄로(각 줄 최대 40자), '\\n' 로 구분\n"
        "- codes: 그 테마에 속하는 종목코드 배열 (같은 재료면 반드시 함께 묶어라)\n"
        "규칙: 같은 정책/이슈(예: 동일 지역개발, 동일 정책 수혜)로 오른 종목은 하나의 테마로 묶는다. "
        "거래대금대장주가 포함된 테마를 우선한다. 한 종목은 한 테마에만.\n\n"
        "반드시 JSON 으로만:\n"
        '{"items":[{"code":"","업종":"","사업":"","특이사항":[],"핵심재료":""}],'
        '"themes":[{"테마":"","요약":"1줄\\n2줄","codes":["",""]}]}\n\n'
        "종목 목록:\n" + "\n\n".join(blocks)
    )

    data = gemini_json(prompt)
    info, themes_raw = {}, []
    if data:
        for it in (data.get("items") or []):
            c = str(it.get("code", "")).strip()
            if c:
                info[c] = it
        themes_raw = data.get("themes") or []
    if not themes_raw:
        print("  [테마] LLM 테마 묶음 실패 → 단일 그룹으로 표시")
        themes_raw = [{"테마": "급등 종목", "요약": "", "codes": [s["코드"] for s in uni]}]

    def enrich(s):
        it = info.get(s["코드"], {})
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
        members.sort(key=lambda x: x["등락률"], reverse=True)
        groups.append({
            "테마": (t.get("테마") or "기타").strip(),
            "요약": (t.get("요약") or "").strip(),
            "종목": members,
            "_시드": any(m["코드"] in seed_codes for m in members),
            "_top": members[0]["등락률"],
        })

    # 미분류 종목 → 기타 급등
    leftover = [enrich(s) for s in uni if s["코드"] not in used]
    if leftover:
        leftover.sort(key=lambda x: x["등락률"], reverse=True)
        groups.append({"테마": "기타 급등주", "요약": "", "종목": leftover,
                       "_시드": False, "_top": leftover[0]["등락률"]})

    # 테마 정렬: 시드 포함 우선 → 최고 상승률
    groups.sort(key=lambda g: (g["_시드"], g["_top"]), reverse=True)
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

def save_dashboard_data(upper: list, theme_map: dict, groups: list):
    """웹 대시보드가 읽을 최신 결과를 data.json 으로 저장."""
    data = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "upper": [{
            "name": s["종목명"], "rate": s["등락률"], "market": s["시장"],
            "theme": (theme_map.get(s["코드"]) or {}).get("theme", ""),
            "amount": round(s.get("거래대금", 0) / 1e8),      # 거래대금(억원)
        } for s in upper],
        "themes": [{
            "theme": g["테마"], "summary": g.get("요약", ""),
            "amount": round(sum(m.get("거래대금", 0) for m in g["종목"]) / 1e8),  # 테마 총 거래대금(억원)
            "stocks": [{
                "name": m["종목명"], "rate": m["등락률"],
                "sector": m.get("업종", ""), "cap": m.get("시총억", 0),
                "flags": m.get("특이사항", []),
                "amount": round(m.get("거래대금", 0) / 1e8),   # 거래대금(억원)
            } for m in g["종목"]],
        } for g in groups],
    }
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  [data] 저장: 상한가 {len(data['upper'])}, 테마 {len(data['themes'])}")
    except Exception as e:
        print(f"  [data] 저장 오류: {e}")


# ─── 장 시간 체크 ─────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 8 * 60 <= t <= 15 * 60 + 30


def is_pre_market() -> bool:
    now = datetime.now()
    t = now.hour * 60 + now.minute
    return 8 * 60 <= t < 9 * 60


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

    # ① 상한가 알림 (+ 테마/뉴스 요약)
    upper = sorted([s for s in stocks if s["등락률"] >= UPPER_LIMIT_THRESHOLD],
                   key=lambda x: x["등락률"], reverse=True)
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
    print("\n단발 실행 완료.")


def main():
    print("=" * 50)
    print("  주식 모니터링 봇 시작 (티마식 테마 분석)")
    print(f"  상한가 기준: {UPPER_LIMIT_THRESHOLD}%  |  주기: {SCAN_INTERVAL//60}분")
    print(f"  Gemini LLM: {'연동됨 ✓' if _gemini_ready() else '미설정 (키 입력 필요) ✗'}")
    print("=" * 50)

    alerted = set()
    upper_theme_map = {}
    last_themes = []
    last_theme_min = -1
    first_run = True

    while True:
        now = datetime.now()
        t = now.hour * 60 + now.minute

        # 주말 → 종료
        if now.weekday() >= 5:
            print(f"[{now.strftime('%H:%M')}] 주말 — 봇 종료")
            break
        # 09:00 이전 → 개장 대기
        if t < 9 * 60:
            print(f"[{now.strftime('%H:%M')}] 개장 전 — 대기...")
            time.sleep(30)
            continue
        # 15:30 이후 → 오늘 세션 종료 (작업 스케줄러가 다음 거래일 09:00에 재시작)
        if t > 15 * 60 + 30:
            print(f"[{now.strftime('%H:%M')}] 장 마감(15:30) — 오늘 세션 종료")
            break

        print(f"\n[{now.strftime('%H:%M')}] 스캔 중...")
        market = fetch_market_data()
        if not market["all"]:
            print("  데이터 없음 — 재시도")
            time.sleep(60)
            continue

        stocks = list(market["all"].values())

        # ① 상한가 — 대시보드용 전체 목록 + 신규만 카톡
        upper_all = sorted([s for s in stocks if s["등락률"] >= UPPER_LIMIT_THRESHOLD],
                           key=lambda x: x["등락률"], reverse=True)
        new_upper = [s for s in upper_all if s["코드"] not in alerted]
        if new_upper:
            tm = classify_upper(new_upper)
            upper_theme_map.update(tm)
            for s in new_upper:
                alerted.add(s["코드"])          # 분석 완료 표시(중복 분석 방지)
            msg = fmt_upper(new_upper, tm)
            print(msg)
            send_kakao(msg)                      # KAKAO_ENABLED=False 면 내부에서 전송 skip
        else:
            print("  상한가 신규 없음")

        # ② 테마 분석 — 첫 실행 즉시, 이후 09~10시 10분마다, 그 외 30분마다
        cur_min = now.hour * 60 + now.minute
        interval = 10 if 9 * 60 <= cur_min < 10 * 60 else 30
        do_theme = first_run or (now.minute % interval == 0 and cur_min != last_theme_min)
        if do_theme:
            groups = analyze_themes(market)
            if groups:
                last_themes = groups
                msg = fmt_theme(groups)
                print(msg)
                send_kakao(msg)
            else:
                print("  테마 조건 충족 종목 없음")
            last_theme_min = cur_min
            first_run = False

        # 대시보드 데이터 저장 (매 스캔)
        save_dashboard_data(upper_all, upper_theme_map, last_themes)

        print(f"  → {SCAN_INTERVAL//60}분 후 재스캔")
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
    else:
        main()
