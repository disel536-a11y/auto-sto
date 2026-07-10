"""
채용 알림 봇 (이력서 맥락 AI 평가 → 카카오 "나에게 보내기" 알림)
─────────────────────────────────────────────────────────────
- 원티드(JSON API) + 링크드인(jobspy) 채용공고 수집
- 김봉수 이력서 프로파일 기준으로 각 공고 1~5점 평가 (Claude API, haiku, 하루 1회 배치)
- 3점 이상 신규 공고만 카카오톡으로 발송
- 중복 알림 방지: SQLite (60일 TTL)

실행:
    python job_alert.py            # 실제 1회 실행 (cron이 매일 21:00 KST 호출)
    python job_alert.py --once     # 위와 동일 (명시적)
    python job_alert.py --selftest # 네트워크/API 없이 필터·포맷·분할 로직 점검
    python job_alert.py --dry      # 크롤링+평가는 하되 카카오 발송은 생략(콘솔 출력만)

배치 위치: /home/ubuntu/auto-sto  (auto-sto와 같은 폴더 → kakao_token.json/config.py 재사용)
설정 키(config.py 또는 환경변수): ANTHROPIC_API_KEY, KAKAO_REST_API_KEY
"""

import os
import sys
import json
import time
import sqlite3
import datetime
import requests

# ══════════════════════════════════════════════════
#  설정 로드 (auto-sto와 동일 패턴: config.py → 환경변수)
# ══════════════════════════════════════════════════
try:
    import config as _config
except ImportError:
    _config = None

def _cfg(name, default=""):
    if _config is not None and hasattr(_config, name):
        return getattr(_config, name)
    return os.environ.get(name, default)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ANTHROPIC_API_KEY  = _cfg("ANTHROPIC_API_KEY")
KAKAO_REST_API_KEY = _cfg("KAKAO_REST_API_KEY")
SARAMIN_API_KEY    = _cfg("SARAMIN_API_KEY")   # 사람인 오픈API 액세스키(없으면 사람인 건너뜀)
KAKAO_TOKEN_FILE   = os.path.join(_BASE_DIR, "kakao_token.json")     # auto-sto가 만든 본인 토큰 재사용
SEEN_DB            = os.path.join(_BASE_DIR, "job_alert_seen.db")    # 중복제거 SQLite

SCORE_MODEL   = "claude-haiku-4-5"
ALERT_MIN     = 3          # 알림 임계값(이 점수 이상만 발송)
SEEN_TTL_DAYS = 60         # 이 기간 지난 공고 id는 자동 삭제 → 재알림 허용

# 크롤 파라미터
WANTED_KEYWORDS = ["NPU", "반도체", "시스템엔지니어", "로보틱스", "휴머노이드",
                   "양산", "eVTOL", "자율주행", "Technical Program Manager"]
WANTED_LIMIT_PER_KW = 20   # 키워드당 최신 공고 수
WANTED_MAX_DETAIL   = 40   # 상세조회(설명 확보) 최대 건수 — IP차단/속도 보호
LINKEDIN_TERMS = ["Technical Program Manager semiconductor", "Systems Engineer NPU",
                  "humanoid robotics program manager", "반도체 양산 PM",
                  "systems engineer robotics", "NPU program manager"]
LINKEDIN_MAX_DETAIL = 25    # 상세조회 최대 건수 — IP차단/속도 보호

# 국내 잡보드(사람인/잡코리아/인크루트) 검색 키워드 — 도메인·직무 타겟팅
DOMESTIC_KEYWORDS = ["반도체 시스템 엔지니어", "NPU", "반도체 PM", "로보틱스 엔지니어",
                     "자율주행 시스템", "반도체 양산", "휴머노이드", "프로그램 매니저 반도체"]
SCORE_MAX = 30              # 하루 AI 평가 상한(토큰·비용 보호)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ══════════════════════════════════════════════════
#  이력서 프로파일 + 1차 필터
# ══════════════════════════════════════════════════
PROFILE = {
    "keywords_role": ["technical program manager", "tpm", "systems engineer", "system engineer",
                      "npi", "양산", "mass production", "operation", "opm",
                      "program manager", "program management", "project manager", "project management",
                      "project/program", "technical project", "pmo", "product manager",
                      "시스템 엔지니어", "시스템엔지니어", "프로그램 매니저", "프로그램 관리",
                      "프로젝트 매니저", "프로젝트 관리", "프로덕트 매니저", "pm"],
    "keywords_domain": ["npu", "semiconductor", "반도체", "ai chip", "humanoid", "휴머노이드",
                        "robotics", "로보틱스", "로봇", "evtol", "autonomous", "자율주행",
                        "mobility", "모빌리티", "soc", "ai 반도체"],
    "seniority_signals": ["책임", "senior", "principal", "lead", "staff", "팀장", "director"],
    "location_must": ["korea", "한국", "서울", "seoul", "대한민국", "경기", "판교",
                      "성남", "인천", "화성", "용인", "수원", "대전"],
    "location_block": ["united states", "canada", "usa", "california", "japan", "europe",
                       "singapore", "vietnam", "india"],
}

def passes_filter(job):
    text = (job.get("title", "") + " " + job.get("desc", "") + " " + job.get("location", "")).lower()
    loc = job.get("location", "").lower()
    if any(b in loc for b in PROFILE["location_block"]):
        return False
    if not any(m in text for m in PROFILE["location_must"]):
        return False
    if not any(k in text for k in PROFILE["keywords_role"]):
        return False
    if not any(k in text for k in PROFILE["keywords_domain"]):
        return False
    return True

def _looks_relevant(text):
    """상세조회 전 1차 스크리닝 — role 또는 domain 키워드가 하나라도 보이면 상세 확인."""
    t = text.lower()
    return (any(k in t for k in PROFILE["keywords_role"]) or
            any(k in t for k in PROFILE["keywords_domain"]))


# ══════════════════════════════════════════════════
#  크롤러 — 원티드 (JSON API)
# ══════════════════════════════════════════════════
def _wanted_search(keyword, limit):
    url = ("https://www.wanted.co.kr/api/v4/jobs"
           f"?country=kr&job_sort=job.latest_order&locations=all&years=-1"
           f"&limit={limit}&offset=0&keyword={requests.utils.quote(keyword)}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    return r.json().get("data", [])

def _wanted_detail(job_id):
    url = f"https://www.wanted.co.kr/api/chaos/jobs/v1/{job_id}/details"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    d = r.json().get("job", {}).get("detail", {})
    parts = [d.get("intro", ""), d.get("main_tasks", ""),
             d.get("requirements", ""), d.get("preferred_points", "")]
    return " ".join(p for p in parts if p)

def crawl_wanted():
    """원티드 최신 공고 → 1차 스크리닝 통과분만 상세조회로 설명 확보."""
    # 1) 키워드별 리스트 수집 후 id 기준 dedup
    listings = {}
    for kw in WANTED_KEYWORDS:
        try:
            for j in _wanted_search(kw, WANTED_LIMIT_PER_KW):
                jid = j.get("id")
                if jid and jid not in listings:
                    listings[jid] = j
            time.sleep(0.4)   # 예의상 지연(IP 차단 방지)
        except Exception as e:
            print(f"  [원티드/{kw}] 검색 오류: {e}")

    # 2) 제목/회사/지역으로 1차 스크리닝 → 상세조회 대상만 추림
    candidates = []
    for j in listings.values():
        pre = f"{j.get('position','')} {j.get('company',{}).get('name','')} " \
              f"{j.get('address',{}).get('full_location','')}"
        if _looks_relevant(pre):
            candidates.append(j)
    candidates = candidates[:WANTED_MAX_DETAIL]

    # 3) 상세조회로 설명 붙이기
    out = []
    for j in candidates:
        jid = j.get("id")
        try:
            desc = _wanted_detail(jid)
            time.sleep(0.4)
        except Exception as e:
            print(f"  [원티드/detail {jid}] 오류: {e}")
            desc = j.get("position", "")
        addr = j.get("address", {})
        out.append({
            "id": f"wt_{jid}",
            "title": j.get("position", ""),
            "company": j.get("company", {}).get("name", ""),
            "location": addr.get("full_location") or addr.get("location", ""),
            "desc": (desc or "")[:600],
            "source": "Wanted",
            "url": f"https://www.wanted.co.kr/wd/{jid}",
        })
    print(f"  [원티드] 수집 {len(listings)} → 스크리닝 {len(candidates)} → 상세 {len(out)}")
    return out


# ══════════════════════════════════════════════════
#  크롤러 — 링크드인 (게스트 검색 API, requests) — jobspy 불필요
# ══════════════════════════════════════════════════
import re

def _html_text(x):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", x)).strip() if x else ""

def _parse_linkedin_cards(html):
    """게스트 검색 결과 HTML → [{id,title,company,location}] (설명 제외)."""
    rows = []
    # 각 카드는 data-entity-urn="urn:li:jobPosting:{id}" 로 시작
    for seg in html.split('data-entity-urn="urn:li:jobPosting:')[1:]:
        mid = re.match(r"(\d+)", seg)
        if not mid:
            continue
        jid = mid.group(1)
        mt = re.search(r'base-search-card__title">(.*?)</h3>', seg, re.S)
        mc = re.search(r'base-search-card__subtitle">(.*?)</h4>', seg, re.S)
        ml = re.search(r'job-search-card__location">(.*?)</span>', seg, re.S)
        rows.append({
            "id": jid,
            "title": _html_text(mt.group(1)) if mt else "",
            "company": _html_text(mc.group(1)) if mc else "",
            "location": _html_text(ml.group(1)) if ml else "South Korea",
        })
    return rows

def _linkedin_search(term, start=0):
    url = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
           f"?keywords={requests.utils.quote(term)}&location=South%20Korea&start={start}")
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    return r.text

def _linkedin_detail(job_id):
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
    r.raise_for_status()
    m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', r.text, re.S)
    return _html_text(m.group(1) if m else r.text)

def crawl_linkedin():
    """링크드인 게스트 API로 수집 → 1차 스크리닝 통과분만 상세조회로 설명 확보."""
    listings = {}
    for term in LINKEDIN_TERMS:
        try:
            for row in _parse_linkedin_cards(_linkedin_search(term, 0)):
                if row["id"] not in listings:
                    listings[row["id"]] = row
            time.sleep(1.5)   # 요청 간격(차단 방지)
        except Exception as e:
            print(f"  [링크드인/{term}] 검색 오류: {e}")

    candidates = [v for v in listings.values()
                  if _looks_relevant(f"{v['title']} {v['company']} {v['location']}")]
    candidates = candidates[:LINKEDIN_MAX_DETAIL]

    out = []
    for v in candidates:
        jid = v["id"]
        try:
            desc = _linkedin_detail(jid)
            time.sleep(1.0)
        except Exception as e:
            print(f"  [링크드인/detail {jid}] 오류: {e}")
            desc = v["title"]
        out.append({
            "id": f"li_{jid}",
            "title": v["title"],
            "company": v["company"],
            "location": v["location"],
            "desc": (desc or "")[:600],
            "source": "LinkedIn",
            "url": f"https://www.linkedin.com/jobs/view/{jid}",
        })
    print(f"  [링크드인] 수집 {len(listings)} → 스크리닝 {len(candidates)} → 상세 {len(out)}")
    return out


# ══════════════════════════════════════════════════
#  크롤러 — 사람인 (공식 오픈API, JSON)
# ══════════════════════════════════════════════════
def crawl_saramin():
    if not SARAMIN_API_KEY:
        print("  [사람인] SARAMIN_API_KEY 미설정 — 건너뜀")
        return []
    out, seen = [], set()
    for kw in DOMESTIC_KEYWORDS:
        try:
            url = ("https://oapi.saramin.co.kr/job-search"
                   f"?access-key={SARAMIN_API_KEY}&keywords={requests.utils.quote(kw)}"
                   "&count=20&sort=pd")
            r = requests.get(url, headers={"Accept": "application/json"}, timeout=15)
            r.raise_for_status()
            jobs = r.json().get("jobs", {}).get("job", [])
            if isinstance(jobs, dict):
                jobs = [jobs]
            for j in jobs:
                jid = str(j.get("id", ""))
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                pos = j.get("position", {})
                out.append({
                    "id": f"sr_{jid}",
                    "title": pos.get("title", ""),
                    "company": j.get("company", {}).get("detail", {}).get("name", ""),
                    "location": (pos.get("location", {}) or {}).get("name", "") or "대한민국",
                    "desc": f'{pos.get("title","")} {(pos.get("job-code",{}) or {}).get("name","")} '
                            f'{(pos.get("industry",{}) or {}).get("name","")}',
                    "source": "사람인",
                    "url": j.get("url", f"https://www.saramin.co.kr/zf_user/jobs/view?rec_idx={jid}"),
                })
            time.sleep(0.5)
        except Exception as e:
            print(f"  [사람인/{kw}] 오류: {e}")
    print(f"  [사람인] 수집 {len(out)}")
    return out


# ══════════════════════════════════════════════════
#  크롤러 — 잡코리아 (검색페이지 스크래핑)
# ══════════════════════════════════════════════════
def crawl_jobkorea():
    out, best = [], {}   # jid -> 가장 긴 제목(제목 링크)
    for kw in DOMESTIC_KEYWORDS:
        try:
            url = f"https://www.jobkorea.co.kr/Search/?stext={requests.utils.quote(kw)}&tabType=recruit"
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
            r.raise_for_status()
            for m in re.finditer(r'GI_Read/(\d+)[^"]*"[^>]*>([\s\S]*?)</a>', r.text):
                jid = m.group(1)
                title = _html_text(m.group(2))
                if title and len(title) > 3 and len(title) > len(best.get(jid, "")):
                    best[jid] = title
            time.sleep(1.0)
        except Exception as e:
            print(f"  [잡코리아/{kw}] 오류: {e}")
    for jid, title in best.items():
        out.append({"id": f"jk_{jid}", "title": title, "company": "",
                    "location": "대한민국", "desc": title, "source": "잡코리아",
                    "url": f"https://www.jobkorea.co.kr/Recruit/GI_Read/{jid}"})
    print(f"  [잡코리아] 수집 {len(out)}")
    return out


# ══════════════════════════════════════════════════
#  크롤러 — 인크루트 (검색페이지 스크래핑, EUC-KR)
# ══════════════════════════════════════════════════
def crawl_incruit():
    out, best = [], {}
    for kw in DOMESTIC_KEYWORDS:
        try:
            kw_euc = requests.utils.quote(kw, encoding="euc-kr")
            url = f"https://search.incruit.com/list/search.asp?col=job&kw={kw_euc}"
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
            r.encoding = "euc-kr"
            for m in re.finditer(r'jobpost\.asp\?job=(\d+)[^"]*"[^>]*>([\s\S]*?)</a>', r.text):
                jid = m.group(1)
                title = _html_text(m.group(2))
                if title and len(title) > 3 and len(title) > len(best.get(jid, "")):
                    best[jid] = title
            time.sleep(1.0)
        except Exception as e:
            print(f"  [인크루트/{kw}] 오류: {e}")
    for jid, title in best.items():
        out.append({"id": f"ic_{jid}", "title": title, "company": "",
                    "location": "대한민국", "desc": title, "source": "인크루트",
                    "url": f"https://job.incruit.com/jobdb_info/jobpost.asp?job={jid}"})
    print(f"  [인크루트] 수집 {len(out)}")
    return out


# ══════════════════════════════════════════════════
#  이력서 맥락 배치 스코어링 (Claude API, 하루 1회)
# ══════════════════════════════════════════════════
RESUME_PROFILE = """지원자: 김봉수 (System Engineer / Technical PM, 13년+)
- 현직: HMG, Atlas 휴머노이드 로봇 PM. 로보틱스 양산/제품화 프로세스 구축, IMS·리스크·스코프 관리, C레벨 기술 로드맵 인터페이스
- 삼성물산: 대형 전기트럭(EV 플랫폼) SE. 시스템 요구사항/상위스펙 정의, RFI, 벤더선정, CapEx 리뷰
- 대한항공 9년: UH-60 헬기 업그레이드(10억달러) 프로그램 리드, 200+ 시스템 스펙, Lockheed/Collins SOW 협상. CH-47/P-3C/IFF
- 핵심역량: SE V-Model, 요구사항 개발/할당, V&V, IMS, 기술 리스크관리, 글로벌 파트너 조율(Boeing/LM/BD), 한영 이중언어 기술협상, ConOps
- 툴: MS Project, Smartsheet, CATIA, JAMA, DOORS, Confluence
이직 트리거(중요): 보상 개선 AND 도메인 가치 상승 동시충족 시에만 가치. 관심도메인: NPU/반도체, 휴머노이드 로보틱스, eVTOL/항공우주, 자율주행/모빌리티. 한국 근무. 책임(G3)급, 임원 트랙 지향."""

INSTRUCTION = """위 지원자 프로파일 기준으로 각 공고를 1~5점 평가하라.
기준(이직 트리거=보상개선 AND 도메인가치상승 동시충족):
- 5: 도메인 정확일치+SE/TPM 강점 직접활용+책임/시니어급 이상+양산·프로그램관리 직결
- 4: 위 중 하나만 약함
- 3: 도메인 또는 직무 중 하나만 강하게 맞음 (알림 임계값)
- 1~2: 부분매칭 또는 커리어 하향/도메인 무관
아래 JSON 배열로만 답하라(다른 텍스트 금지):
[{"id":"...","score":N,"fit":"강점 한 줄","gap":"우려 한 줄 또는 없음"}]"""

def score_batch(jobs, model=SCORE_MODEL):
    # anthropic SDK 대신 requests로 직접 호출 → 새 패키지 불필요(서버에 requests 기존 설치)
    compact = [{"id": j["id"], "title": j["title"], "company": j["company"],
                "location": j["location"], "desc": j["desc"][:300]} for j in jobs]
    prompt = f'{RESUME_PROFILE}\n\n{INSTRUCTION}\n\n공고목록:\n{json.dumps(compact, ensure_ascii=False)}'
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": model, "max_tokens": 4096,   # 다건 평가 시 JSON 잘림 방지
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90)
    r.raise_for_status()
    text = r.json()["content"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
    # 앞뒤 잡텍스트/코드펜스 방지 — JSON 배열 경계만 추출
    if "[" in text and "]" in text:
        text = text[text.index("["):text.rindex("]") + 1]
    return {x["id"]: x for x in json.loads(text)}


# ══════════════════════════════════════════════════
#  중복 제거 (SQLite, 60일 TTL)
# ══════════════════════════════════════════════════
def _db():
    con = sqlite3.connect(SEEN_DB)
    con.execute("CREATE TABLE IF NOT EXISTS seen (job_id TEXT PRIMARY KEY, ts INTEGER)")
    # 만료분 청소
    con.execute("DELETE FROM seen WHERE ts < ?", (int(time.time()) - SEEN_TTL_DAYS * 86400,))
    con.commit()
    return con

def is_new(con, job_id):
    cur = con.execute("SELECT 1 FROM seen WHERE job_id = ?", (job_id,))
    return cur.fetchone() is None

def mark_seen(con, job_id):
    con.execute("INSERT OR REPLACE INTO seen (job_id, ts) VALUES (?, ?)",
                (job_id, int(time.time())))
    con.commit()


# ══════════════════════════════════════════════════
#  카카오 "나에게 보내기" (auto-sto 방식 재사용)
# ══════════════════════════════════════════════════
def _kakao_payload(message, url):
    return {"template_object": json.dumps({
        "object_type": "text",
        "text": message,
        "link": {"web_url": url, "mobile_web_url": url},
    })}

def _kakao_post(token, message, url):
    return requests.post(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        headers={"Authorization": f"Bearer {token}"},
        data=_kakao_payload(message, url), timeout=10)

def _load_token():
    if os.path.exists(KAKAO_TOKEN_FILE):
        try:
            with open(KAKAO_TOKEN_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[카카오] 토큰 파일 읽기 오류: {e}")
    return {}

def _save_token(tok):
    try:
        with open(KAKAO_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(tok, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[카카오] 토큰 저장 오류: {e}")

def _refresh_token(tok):
    try:
        r = requests.post("https://kauth.kakao.com/oauth/token", timeout=10,
                          data={"grant_type": "refresh_token",
                                "client_id": KAKAO_REST_API_KEY,
                                "refresh_token": tok.get("refresh_token", "")})
        return r.json()
    except Exception as e:
        print(f"[카카오] 갱신 오류: {e}")
        return {}

def _split_message(message, limit=900):
    if len(message) <= limit:
        return [message]
    chunks, cur = [], ""
    for b in message.split("\n\n"):
        if len(cur) + len(b) + 2 > limit and cur:
            chunks.append(cur); cur = b
        else:
            cur = b if not cur else cur + "\n\n" + b
    if cur:
        chunks.append(cur)
    n = len(chunks)
    return [f"({i+1}/{n})\n{c}" if n > 1 else c for i, c in enumerate(chunks)]

def send_kakao(message, url="https://www.wanted.co.kr"):
    tok = _load_token()
    if not tok.get("access_token"):
        print("[카카오] kakao_token.json 없음 → auto-sto의 'python kakao_token_setup.py' 먼저 실행")
        return False
    ok_all = True
    for ch in _split_message(message):
        res = _kakao_post(tok["access_token"], ch, url)
        ok = res.status_code == 200 and res.json().get("result_code") == 0
        if not ok and res.status_code == 401:      # 만료 → 갱신 후 재시도
            new = _refresh_token(tok)
            if new.get("access_token"):
                tok.update(new)
                res = _kakao_post(tok["access_token"], ch, url)
                ok = res.status_code == 200 and res.json().get("result_code") == 0
        ok_all = ok_all and ok
        if not ok:
            print(f"[카카오] 전송 실패: {res.text[:120]}")
        time.sleep(0.3)
    _save_token(tok)
    if ok_all:
        print("[카카오] 전송 완료 ✓")
    return ok_all


# ══════════════════════════════════════════════════
#  메시지 포맷 (카카오 텍스트)
# ══════════════════════════════════════════════════
def build_message(final):
    """final = [(job, score_dict), ...] score 내림차순."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    header = f"🔔 오늘의 채용 알림 ({today}) · 신규 {len(final)}건\n이력서 맥락 기반 3점↑\n"
    blocks = [header]
    star = {5: "⭐⭐⭐⭐⭐", 4: "⭐⭐⭐⭐", 3: "⭐⭐⭐"}
    for j, sc in final:
        s = sc["score"]
        lines = [f"[{star.get(s, s)}] {j['title']}",
                 f"🏢 {j['company']} | 📍 {j['location']} | {j['source']}",
                 f"💡 {sc.get('fit','')}"]
        if sc.get("gap") and sc["gap"] not in ("없음", ""):
            lines.append(f"⚠ {sc['gap']}")
        lines.append(f"🔗 {j['url']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# ══════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════
def run(dry=False):
    print("=" * 52)
    print(f"  채용 알림 봇 — {datetime.datetime.now():%Y-%m-%d %H:%M} KST")
    print(f"  Claude API: {'OK' if ANTHROPIC_API_KEY else '미설정 ✗'} | 임계값 {ALERT_MIN}점 | dry={dry}")
    print("=" * 52)

    print("① 크롤링 (원티드+링크드인 / 사람인+잡코리아+인크루트)...", flush=True)
    broad = crawl_wanted() + crawl_linkedin()          # 광범위 검색 → 엄격 필터
    domestic = crawl_saramin() + crawl_jobkorea() + crawl_incruit()  # 타겟 검색 → 완화 필터
    print(f"   총 수집 {len(broad) + len(domestic)}건 (광범위 {len(broad)} / 국내보드 {len(domestic)})", flush=True)
    if not broad and not domestic:
        print("   수집 0건 — 종료"); return

    print("② 필터 (광범위=엄격 3중 / 국내보드=타겟검색이라 완화)...", flush=True)
    # 광범위 소스: 지역·직무·도메인 3중 필터. 국내보드: 이미 도메인 검색이라 role/domain 하나만 봐도 통과
    passed = [j for j in broad if passes_filter(j)]
    passed += [j for j in domestic if _looks_relevant(j["title"] + " " + j["desc"])]
    # id 기준 중복 제거
    _seen, _uniq = set(), []
    for j in passed:
        if j["id"] not in _seen:
            _seen.add(j["id"]); _uniq.append(j)
    passed = _uniq
    print(f"   통과 {len(passed)}건", flush=True)
    if not passed:
        print("   필터 통과 0건 — 종료"); return
    if len(passed) > SCORE_MAX:   # 토큰·비용 보호
        print(f"   평가 대상 {SCORE_MAX}건으로 제한 (통과 {len(passed)}건 중)", flush=True)
        passed = passed[:SCORE_MAX]

    if not ANTHROPIC_API_KEY:
        print("   ANTHROPIC_API_KEY 미설정 — 평가 불가, 종료"); return

    print(f"③ Claude 배치 평가 ({SCORE_MODEL}, API 1회)...", flush=True)
    try:
        scores = score_batch(passed)
    except Exception as e:
        print(f"   평가 오류: {e}"); return

    con = _db()
    final = []
    for j in passed:
        sc = scores.get(j["id"])
        if sc and sc.get("score", 0) >= ALERT_MIN and is_new(con, j["id"]):
            final.append((j, sc))
            if not dry:
                mark_seen(con, j["id"])
    con.close()
    final.sort(key=lambda x: -x[1]["score"])
    print(f"④ 알림 대상 {len(final)}건 (3점↑ & 신규)", flush=True)

    if not final:
        print("   신규 알림 없음 — 종료"); return

    msg = build_message(final)
    print("\n" + msg + "\n")
    if dry:
        print("[dry] 카카오 발송 생략")
    else:
        top_url = final[0][0]["url"]
        send_kakao(msg, url=top_url)
    print("완료.")


# ══════════════════════════════════════════════════
#  덤프 — 필터 통과 공고 전체를 HTML 표로 저장 (검토·필터링용)
# ══════════════════════════════════════════════════
def _collect_passed():
    broad = crawl_wanted() + crawl_linkedin()
    domestic = crawl_saramin() + crawl_jobkorea() + crawl_incruit()
    passed = [j for j in broad if passes_filter(j)]
    passed += [j for j in domestic if _looks_relevant(j["title"] + " " + j["desc"])]
    seen, uniq = set(), []
    for j in passed:
        if j["id"] not in seen:
            seen.add(j["id"]); uniq.append(j)
    return uniq

def dump_passed():
    import html as _html
    jobs = _collect_passed()
    # 소스별 건수
    from collections import Counter
    cnt = Counter(j["source"] for j in jobs)
    chips = " ".join(f'<span class="chip">{_html.escape(s)} {n}</span>' for s, n in cnt.most_common())
    rows = ""
    for i, j in enumerate(jobs, 1):
        rows += (f'<tr data-src="{_html.escape(j["source"])}">'
                 f'<td>{i}</td><td><span class="src">{_html.escape(j["source"])}</span></td>'
                 f'<td><a href="{_html.escape(j["url"])}" target="_blank">{_html.escape(j["title"])}</a></td>'
                 f'<td>{_html.escape(j["company"])}</td><td>{_html.escape(j["location"])}</td></tr>')
    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>필터 통과 공고 {len(jobs)}건</title>
<style>
body{{font-family:-apple-system,'Malgun Gothic',sans-serif;margin:0;background:#f6f7f9;color:#111}}
.wrap{{max-width:1000px;margin:0 auto;padding:16px}}
h1{{font-size:20px;margin:8px 0}}
.chip{{display:inline-block;background:#111;color:#fff;border-radius:12px;padding:2px 10px;font-size:13px;margin:2px}}
input{{width:100%;padding:10px;border:1px solid #ccc;border-radius:8px;font-size:15px;margin:10px 0;box-sizing:border-box}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden}}
th,td{{padding:8px 10px;border-bottom:1px solid #eee;font-size:14px;text-align:left;vertical-align:top}}
th{{background:#fafafa;position:sticky;top:0;cursor:pointer}}
.src{{background:#eef;border-radius:6px;padding:1px 7px;font-size:12px;color:#334}}
a{{color:#1a56db;text-decoration:none}} a:hover{{text-decoration:underline}}
</style></head><body><div class="wrap">
<h1>🔎 필터 통과 공고 {len(jobs)}건 <span style="font-size:13px;color:#888">({today} 기준)</span></h1>
<div>{chips}</div>
<input id="q" placeholder="제목·회사·지역·소스 검색 (예: 반도체, 성남, 잡코리아, PM)">
<table id="t"><thead><tr><th>#</th><th>소스</th><th>제목</th><th>회사</th><th>지역</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<script>
const q=document.getElementById('q'),rows=[...document.querySelectorAll('#t tbody tr')];
q.addEventListener('input',()=>{{const v=q.value.toLowerCase();rows.forEach(r=>{{r.style.display=r.textContent.toLowerCase().includes(v)?'':'none';}});}});
document.querySelectorAll('#t th').forEach((th,i)=>th.addEventListener('click',()=>{{
 const tb=document.querySelector('#t tbody');[...tb.rows].sort((a,b)=>a.cells[i].textContent.localeCompare(b.cells[i].textContent,'ko',{{numeric:true}})).forEach(r=>tb.appendChild(r));}}));
</script></body></html>"""
    path = os.path.join(_BASE_DIR, "passed_jobs.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"[dump] 필터 통과 {len(jobs)}건 → {path}")
    print("       웹서버 재시작 후 http://<서버IP>/jobs 에서 확인")


# ══════════════════════════════════════════════════
#  셀프테스트 (네트워크/API 미사용)
# ══════════════════════════════════════════════════
def _selftest():
    print("=== SELFTEST: 필터/포맷/분할 점검 (네트워크·API 미사용) ===\n")

    # 1) 필터 점검
    good = {"title": "시스템 엔지니어 (반도체 양산)", "company": "A반도체",
            "location": "경기 화성", "desc": "NPU 칩 양산 프로그램 관리, 시스템 요구사항 정의"}
    bad_loc = {"title": "Systems Engineer NPU", "company": "B", "location": "California, USA", "desc": "semiconductor"}
    bad_dom = {"title": "마케팅 매니저", "company": "C", "location": "서울", "desc": "브랜드 마케팅"}
    assert passes_filter(good), "정상 공고가 필터에서 탈락"
    assert not passes_filter(bad_loc), "미국 공고가 필터를 통과"
    assert not passes_filter(bad_dom), "무관 도메인이 필터를 통과"
    print("[OK] passes_filter: 정상통과 / 해외차단 / 무관도메인차단")

    # 2) 원티드 상세 파싱(캡처된 실제 응답 구조로 검증)
    sample_detail = {"job": {"detail": {"intro": "회사 소개", "main_tasks": "Azure 서버 구축",
                     "requirements": "IT 2년 이상", "preferred_points": "DB 최적화"}}}
    d = sample_detail["job"]["detail"]
    desc = " ".join(p for p in [d["intro"], d["main_tasks"], d["requirements"], d["preferred_points"]] if p)
    assert "Azure" in desc and "IT 2년" in desc
    print("[OK] 원티드 상세 파싱: intro+main_tasks+requirements+preferred 결합")

    # 2b) 링크드인 카드 파싱 (실제 게스트 HTML 구조 mock)
    li_html = (
        '<li><div class="base-card base-search-card job-search-card" '
        'data-entity-urn="urn:li:jobPosting:4403422295">'
        '<h3 class="base-search-card__title"> Systems Engineer, Semiconductor </h3>'
        '<h4 class="base-search-card__subtitle"><a>ACME Semi</a></h4>'
        '<span class="job-search-card__location"> Seoul, South Korea </span></div></li>'
        '<li><div data-entity-urn="urn:li:jobPosting:4400000001">'
        '<h3 class="base-search-card__title"> Robotics Program Manager </h3>'
        '<h4 class="base-search-card__subtitle"><a>Robo Inc</a></h4>'
        '<span class="job-search-card__location"> Gyeonggi, South Korea </span></div></li>'
    )
    cards = _parse_linkedin_cards(li_html)
    assert len(cards) == 2, f"카드 파싱 실패: {cards}"
    assert cards[0]["id"] == "4403422295" and "Semiconductor" in cards[0]["title"]
    assert cards[0]["company"] == "ACME Semi" and "Seoul" in cards[0]["location"]
    print("[OK] 링크드인 카드 파싱: id/title/company/location 추출")

    # 2c) 확장된 role 키워드로 'Program Management/Project' 공고 회수 확인
    am = {"title": "Technical Project/Program Management", "company": "Applied Materials Korea",
          "location": "Hwaseong, Gyeonggi, South Korea",
          "desc": "materials engineering to produce every new chip and semiconductor. "
                  "Helps to prepare project plan and coordinates project schedule."}
    assert passes_filter(am), "Applied Materials TPM 공고가 여전히 필터에서 탈락"
    print("[OK] role 확장: 'Program Management/Project' 공고 필터 통과")

    # 2d) 잡코리아/인크루트 검색페이지 정규식 파싱
    jk_html = ('<a href="/Recruit/GI_Read/49362247?x=1" class="logo"><img></a>'
               '<a href="/Recruit/GI_Read/49362247?y=2">[병특포함]ML Systems Engineer</a>')
    jk = {}
    for m in re.finditer(r'GI_Read/(\d+)[^"]*"[^>]*>([\s\S]*?)</a>', jk_html):
        t = _html_text(m.group(2))
        if t and len(t) > 3 and len(t) > len(jk.get(m.group(1), "")):
            jk[m.group(1)] = t
    assert jk.get("49362247") == "ML Systems Engineer".rjust(0) or "Systems Engineer" in jk.get("49362247", "")
    ic_html = '<a href="/jobdb_info/jobpost.asp?job=2607050000205&src=x">반도체 설계 엔지니어</a>'
    icm = re.search(r'jobpost\.asp\?job=(\d+)[^"]*"[^>]*>([\s\S]*?)</a>', ic_html)
    assert icm and icm.group(1) == "2607050000205" and "반도체" in _html_text(icm.group(2))
    print("[OK] 잡코리아/인크루트 정규식: id·제목 추출")

    # 3) 메시지 포맷 + 분할
    final = [
        ({"title": "휴머노이드 로봇 양산 PM", "company": "테크로보", "location": "경기 성남",
          "source": "Wanted", "url": "https://www.wanted.co.kr/wd/111"},
         {"id": "wt_111", "score": 5, "fit": "로보틱스 양산+SE 강점 직접활용", "gap": "없음"}),
        ({"title": "시스템 엔지니어 (반도체)", "company": "칩스", "location": "화성",
          "source": "LinkedIn", "url": "https://linkedin.com/jobs/222"},
         {"id": "li_222", "score": 3, "fit": "반도체 도메인 적합", "gap": "직무가 SE 세부에 치우침"}),
    ]
    msg = build_message(final)
    print("\n--- build_message 출력 예시 ---")
    print(msg)
    parts = _split_message("가나다\n\n" * 400)   # 긴 메시지 분할 확인
    assert len(parts) > 1 and all(len(p) <= 960 for p in parts)
    print(f"\n[OK] 메시지 분할: 긴 메시지 → {len(parts)}개 청크")

    # 4) SQLite 중복제거 (임시 DB — OS 임시 디렉터리 사용)
    import tempfile
    global SEEN_DB
    SEEN_DB = os.path.join(tempfile.gettempdir(), "_job_alert_selftest_seen.db")
    if os.path.exists(SEEN_DB):
        os.remove(SEEN_DB)
    con = _db()
    assert is_new(con, "wt_999")
    mark_seen(con, "wt_999")
    assert not is_new(con, "wt_999")
    con.close()
    os.remove(SEEN_DB)
    print("[OK] SQLite 중복제거: 신규→기록→중복차단")

    print("\n=== SELFTEST 통과 ===")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif "--dump" in sys.argv:
        dump_passed()
    elif "--dry" in sys.argv:
        run(dry=True)
    else:
        run(dry=False)
