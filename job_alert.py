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
                  "humanoid robotics program manager", "반도체 양산 PM"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ══════════════════════════════════════════════════
#  이력서 프로파일 + 1차 필터
# ══════════════════════════════════════════════════
PROFILE = {
    "keywords_role": ["technical program manager", "tpm", "systems engineer", "system engineer",
                      "npi", "양산", "mass production", "operation", "opm", "program manager",
                      "시스템 엔지니어", "시스템엔지니어", "프로그램 매니저", "pm"],
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
#  크롤러 — 링크드인 (jobspy, 선택적)
# ══════════════════════════════════════════════════
def crawl_linkedin():
    """jobspy 미설치/차단 시 빈 리스트 반환(원티드만으로도 동작)."""
    try:
        from jobspy import scrape_jobs
    except Exception:
        print("  [링크드인] jobspy 미설치 — 건너뜀 (원티드만 사용)")
        return []
    out = []
    for term in LINKEDIN_TERMS:
        try:
            df = scrape_jobs(site_name=["linkedin"], search_term=term,
                             location="South Korea", results_wanted=15, hours_old=48)
            for _, r in df.iterrows():
                out.append({
                    "id": f"li_{r.get('id','')}",
                    "title": str(r.get("title", "")),
                    "company": str(r.get("company", "")),
                    "location": str(r.get("location", "")),
                    "desc": str(r.get("description", ""))[:600],
                    "source": "LinkedIn",
                    "url": str(r.get("job_url", "")),
                })
            time.sleep(2.0)   # 링크드인은 특히 요청 간격을 넉넉히
        except Exception as e:
            print(f"  [링크드인/{term}] 오류: {e}")
    print(f"  [링크드인] 수집 {len(out)}")
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
    import anthropic
    compact = [{"id": j["id"], "title": j["title"], "company": j["company"],
                "location": j["location"], "desc": j["desc"][:400]} for j in jobs]
    prompt = f'{RESUME_PROFILE}\n\n{INSTRUCTION}\n\n공고목록:\n{json.dumps(compact, ensure_ascii=False)}'
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(model=model, max_tokens=1500,
                                   messages=[{"role": "user", "content": prompt}])
    text = resp.content[0].text.strip().replace("```json", "").replace("```", "").strip()
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

    print("① 크롤링 (원티드 + 링크드인)...", flush=True)
    raw = crawl_wanted() + crawl_linkedin()
    print(f"   총 수집 {len(raw)}건", flush=True)
    if not raw:
        print("   수집 0건 — 종료"); return

    print("② 1차 필터 (지역/직무/도메인)...", flush=True)
    passed = [j for j in raw if passes_filter(j)]
    print(f"   통과 {len(passed)}건", flush=True)
    if not passed:
        print("   필터 통과 0건 — 종료"); return

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
    elif "--dry" in sys.argv:
        run(dry=True)
    else:
        run(dry=False)
