"""
주식봇 웹 대시보드 서버.
봇이 저장한 data.json 을 읽어 대시보드(dashboard.html)로 보여줍니다.
로그인(다중 유저) + 관리자 페이지(/admin) 포함.
실행: python3 app.py   (기본 포트 80)

최초 관리자 계정 만들기:
    python3 auth.py create-admin <아이디> <비밀번호>
"""
import os
import sys
import json
import time
import secrets
import subprocess
from functools import wraps
from flask import (Flask, send_file, Response, jsonify, request,
                   session, redirect, url_for, render_template_string)

import auth
import board

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE, "data.json")
ALERT_FILE = os.path.join(BASE, "alert.json")         # KRX 시장경보(투경 탭)
REFRESH_FLAG = os.path.join(BASE, "refresh.flag")     # 봇 루프가 감지 → 전체 재분석
HEARTBEAT_FILE = os.path.join(BASE, "bot.heartbeat")  # 봇 루프 생존 신호
REFRESH_LOCK = os.path.join(BASE, "refresh.lock")     # 장외 --once 중복 실행 방지
SECRET_FILE = os.path.join(BASE, "secret.key")

app = Flask(__name__)


def _load_secret_key() -> str:
    """세션 서명 키. config.SECRET_KEY → secret.key 파일(자동 생성) 순."""
    try:
        import config
        k = getattr(config, "SECRET_KEY", None)
        if k:
            return k
    except Exception:
        pass
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE) as f:
            return f.read().strip()
    k = secrets.token_hex(32)
    try:
        with open(SECRET_FILE, "w") as f:
            f.write(k)
        os.chmod(SECRET_FILE, 0o600)
    except Exception:
        pass
    return k


app.secret_key = _load_secret_key()


def _https_only() -> bool:
    """HTTPS 전용 쿠키 여부. 기본 True. http 로 되돌리려면 config.HTTPS_ONLY=False."""
    try:
        import config
        return bool(getattr(config, "HTTPS_ONLY", True))
    except Exception:
        return True


app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_https_only(),           # https 로만 쿠키 전송
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,  # 14일
)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.headers.get("X-Forwarded-Proto") == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


# ── 간단한 요청 빈도 제한 (무차별 대입/스팸 방지, 메모리 기반) ──
_RL = {}


def _rate_ok(key, limit, window_sec) -> bool:
    now = time.time()
    lst = [t for t in _RL.get(key, []) if now - t < window_sec]
    ok = len(lst) < limit
    if ok:
        lst.append(now)
    _RL[key] = lst
    return ok
auth.init_db()
board.init_db()


# ── 헬퍼 ────────────────────────────────────────────────
def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    u = auth.get_user_by_id(uid)
    if not u or u["status"] != "active":
        session.clear()
        return None
    return u


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "")


def login_required(f):
    @wraps(f)
    def wrap(*a, **k):
        if not current_user():
            if request.path.startswith("/api/"):
                return jsonify(status="unauthorized"), 401
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return wrap


def admin_required(f):
    @wraps(f)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        if not auth.is_admin(u):                      # admin 또는 owner
            if request.path.startswith("/api/"):
                return jsonify(status="forbidden"), 403
            return Response("권한이 없습니다 (관리자 전용).", status=403,
                            mimetype="text/plain; charset=utf-8")
        return f(*a, **k)
    return wrap


def owner_required(f):
    @wraps(f)
    def wrap(*a, **k):
        u = current_user()
        if not u:
            return redirect(url_for("login", next=request.path))
        if not auth.is_owner(u):
            return jsonify(status="forbidden", detail="공지 작성 권한이 없습니다."), 403
        return f(*a, **k)
    return wrap


# ── 인증 라우트 ─────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if not _rate_ok(("login", _client_ip()), 15, 600):   # IP당 10분에 15회
            error = "시도가 너무 많습니다. 잠시 후 다시 시도하세요."
            return render_template_string(LOGIN_HTML, error=error, mode="login")
        un = request.form.get("username", "")
        pw = request.form.get("password", "")
        u, err = auth.verify_login(un, pw)
        auth.log_login(un, _client_ip(), request.headers.get("User-Agent"), bool(u))
        if u:
            session.clear()                       # 세션 고정 방지: 새 세션으로 시작
            session.permanent = True
            session["uid"] = u["id"]
            nxt = request.args.get("next") or url_for("index")
            # '//evil.com' 같은 프로토콜 상대 주소로의 납치 차단
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("index")
            return redirect(nxt)
        error = err
    return render_template_string(LOGIN_HTML, error=error, mode="login")


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))
    error = None
    done = False
    if request.method == "POST":
        if not _rate_ok(("reg", _client_ip()), 5, 3600):     # IP당 1시간에 5회
            error = "가입 시도가 너무 많습니다. 잠시 후 다시 시도하세요."
            return render_template_string(LOGIN_HTML, error=error, mode="register", done=False)
        un = request.form.get("username", "")
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if pw != pw2:
            error = "비밀번호가 일치하지 않습니다."
        else:
            uid, err = auth.create_user(un, pw)  # 기본 status=pending
            if err:
                error = err
            else:
                done = True
    return render_template_string(LOGIN_HTML, error=error, mode="register", done=done)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── 대시보드 ────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    u = current_user()
    auth.log_pageview(u, "/", _client_ip(), request.headers.get("User-Agent"))
    return send_file(os.path.join(BASE, "dashboard.html"))


@app.route("/api/data")
@login_required
def api_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            return Response(f.read(), mimetype="application/json; charset=utf-8")
    empty = {"updated": "", "upper": [], "themes": []}
    return Response(json.dumps(empty, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.route("/api/alert")
@login_required
def api_alert():
    """KRX 시장경보(투자경고/투자위험/단기과열). krx_alert.py 가 매일 저장."""
    if os.path.exists(ALERT_FILE):
        with open(ALERT_FILE, encoding="utf-8") as f:
            return Response(f.read(), mimetype="application/json; charset=utf-8")
    empty = {"updated": "", "trade_date": "", "warning": [], "danger": [],
             "overheat": [], "counts": {"warning": 0, "danger": 0, "overheat": 0}}
    return Response(json.dumps(empty, ensure_ascii=False), mimetype="application/json; charset=utf-8")


@app.route("/api/me")
@login_required
def api_me():
    u = current_user()
    return jsonify(username=u["username"], role=u["role"],
                   is_admin=auth.is_admin(u), is_owner=auth.is_owner(u))


# ── 공지 & 문의 게시판 ──────────────────────────────────
@app.route("/api/board")
@login_required
def api_board():
    """공지 + 문의(댓글 포함) 전체. 문의는 전체 공개."""
    return jsonify(notices=board.list_notices(), posts=board.list_posts())


@app.route("/api/board/notice", methods=["POST"])
@admin_required
def api_notice_create():
    u = current_user()
    nid, err = board.create_notice(request.form.get("title"),
                                   request.form.get("body"), u["username"])
    return (jsonify(status="error", detail=err), 400) if err else jsonify(status="ok", id=nid)


@app.route("/api/board/notice/<int:nid>", methods=["POST"])
@admin_required
def api_notice_edit(nid):
    action = request.form.get("action", "edit")
    if action == "delete":
        return jsonify(status="ok" if board.delete_notice(nid) else "error")
    err = board.update_notice(nid, request.form.get("title"), request.form.get("body"))
    return (jsonify(status="error", detail=err), 400) if err else jsonify(status="ok")


@app.route("/api/board/post", methods=["POST"])
@login_required
def api_post_create():
    u = current_user()
    if not _rate_ok(("post", u["id"]), 10, 3600):            # 유저당 1시간에 10개
        return jsonify(status="error", detail="작성이 너무 잦습니다. 잠시 후 다시 시도하세요."), 429
    pid, err = board.create_post(u, request.form.get("title"), request.form.get("body"))
    return (jsonify(status="error", detail=err), 400) if err else jsonify(status="ok", id=pid)


@app.route("/api/board/post/<int:pid>/delete", methods=["POST"])
@login_required
def api_post_delete(pid):
    u = current_user()
    p = board.get_post(pid)
    if not p:
        return jsonify(status="error", detail="없는 글입니다."), 404
    # 작성 본인 또는 관리자만 삭제
    if p["user_id"] != u["id"] and not auth.is_admin(u):
        return jsonify(status="forbidden", detail="삭제 권한이 없습니다."), 403
    return jsonify(status="ok" if board.delete_post(pid) else "error")


@app.route("/api/board/post/<int:pid>/comment", methods=["POST"])
@admin_required
def api_comment_create(pid):
    """문의 댓글 = admin/owner 전용."""
    u = current_user()
    if not _rate_ok(("cmt", u["id"]), 30, 3600):
        return jsonify(status="error", detail="작성이 너무 잦습니다."), 429
    cid, err = board.add_comment(pid, u, request.form.get("body"))
    return (jsonify(status="error", detail=err), 400) if err else jsonify(status="ok", id=cid)


@app.route("/api/board/comment/<int:cid>/delete", methods=["POST"])
@admin_required
def api_comment_delete(cid):
    return jsonify(status="ok" if board.delete_comment(cid) else "error")


def _bot_alive() -> bool:
    """봇 루프가 최근 120초 내 heartbeat 를 남겼으면 가동 중으로 판단."""
    try:
        return os.path.exists(HEARTBEAT_FILE) and (time.time() - os.path.getmtime(HEARTBEAT_FILE)) < 120
    except OSError:
        return False


@app.route("/api/refresh", methods=["POST"])
@login_required
def api_refresh():
    """수동 새로고침 — 상한가+테마를 raw 부터 전체 재분석."""
    if _bot_alive():
        try:
            with open(REFRESH_FLAG, "w") as f:
                f.write(str(time.time()))
            return jsonify(status="queued")
        except Exception as e:
            return jsonify(status="error", detail=str(e)), 500

    if os.path.exists(REFRESH_LOCK) and (time.time() - os.path.getmtime(REFRESH_LOCK)) < 90:
        return jsonify(status="busy")
    try:
        with open(REFRESH_LOCK, "w") as f:
            f.write(str(time.time()))
        subprocess.Popen([sys.executable, os.path.join(BASE, "stock_monitor.py"), "--once"], cwd=BASE)
        return jsonify(status="started")
    except Exception as e:
        return jsonify(status="error", detail=str(e)), 500


# ── 관리자 ──────────────────────────────────────────────
@app.route("/admin")
@admin_required
def admin():
    return render_template_string(ADMIN_HTML)


@app.route("/api/admin/overview")
@admin_required
def admin_overview():
    return jsonify(users=auth.list_users(),
                   counts=auth.count_users(),
                   stats=auth.stats(),
                   bot_alive=_bot_alive())


@app.route("/api/admin/user/<int:uid>", methods=["POST"])
@admin_required
def admin_user(uid):
    me = current_user()
    action = request.form.get("action", "")
    # owner 계정은 웹에서 변경·삭제 불가(잠금 방지). owner 관리는 CLI 로만.
    target = auth.get_user_by_id(uid)
    if target and target.get("role") == "owner" and uid != me["id"]:
        return jsonify(status="error", detail="owner 계정은 여기서 변경할 수 없습니다."), 400
    ok = False
    if action == "approve":
        ok = auth.set_status(uid, "active")
    elif action == "disable":
        if uid == me["id"]:
            return jsonify(status="error", detail="본인 계정은 비활성화할 수 없습니다."), 400
        ok = auth.set_status(uid, "disabled")
    elif action == "make-admin":
        ok = auth.set_role(uid, "admin")
    elif action == "make-user":
        if uid == me["id"]:
            return jsonify(status="error", detail="본인 관리자 권한은 해제할 수 없습니다."), 400
        ok = auth.set_role(uid, "user")
    elif action == "delete":
        if uid == me["id"]:
            return jsonify(status="error", detail="본인 계정은 삭제할 수 없습니다."), 400
        ok = auth.delete_user(uid)
    else:
        return jsonify(status="error", detail="알 수 없는 동작"), 400
    return jsonify(status="ok" if ok else "error")


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="theme-color" content="#0d0f12">
<title>마켓중심 · {{ '가입' if mode=='register' else '로그인' }}</title>
<style>
  :root{--bg:#0d0f12;--scr:#15171c;--s1:#1e2128;--bd:rgba(255,255,255,.09);
    --tp:#e9eaee;--ts:#a1a4ac;--tm:#6c7079;--suc:#37c07d;--up:#ff6b66;--dn:#4c8dff;}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
  body{margin:0;background:var(--bg);color:var(--tp);min-height:100vh;display:flex;
    align-items:center;justify-content:center;padding:20px;
    font-family:-apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕",sans-serif;}
  .box{width:100%;max-width:340px;background:var(--scr);border:0.5px solid var(--bd);
    border-radius:16px;padding:26px 22px;}
  h1{font-size:20px;font-weight:700;margin:0 0 4px;display:flex;align-items:center;gap:8px;}
  .sub{font-size:12.5px;color:var(--tm);margin:0 0 18px;}
  label{display:block;font-size:12px;color:var(--ts);margin:12px 0 5px;}
  input{width:100%;padding:11px 12px;background:var(--s1);border:0.5px solid var(--bd);
    border-radius:9px;color:var(--tp);font-size:15px;font-family:inherit;}
  input:focus{outline:none;border-color:var(--dn);}
  button{width:100%;margin-top:18px;padding:12px;border:0;border-radius:9px;
    background:var(--dn);color:#fff;font-size:15px;font-weight:600;cursor:pointer;font-family:inherit;}
  button:active{opacity:.85;}
  .err{background:rgba(255,107,102,.14);border:0.5px solid var(--up);color:var(--up);
    font-size:12.5px;padding:9px 11px;border-radius:9px;margin-bottom:6px;}
  .ok{background:rgba(55,192,125,.14);border:0.5px solid var(--suc);color:var(--suc);
    font-size:12.5px;padding:11px 12px;border-radius:9px;line-height:1.55;}
  .alt{text-align:center;font-size:12.5px;color:var(--tm);margin-top:16px;}
  .alt a{color:var(--dn);text-decoration:none;font-weight:600;}
  .logo{width:22px;height:22px;}
</style></head><body>
<div class="box">
  <h1><svg class="logo" viewBox="0 0 24 24" fill="none" stroke="var(--up)" stroke-width="2.2"
      stroke-linecap="round" stroke-linejoin="round"><polyline points="3 16 9 10 13 14 21 6"/>
      <polyline points="21 11 21 6 16 6"/></svg>마켓중심</h1>
  {% if mode=='register' and done %}
    <div class="ok">가입이 완료되었습니다.<br>이제 로그인하여 바로 이용할 수 있습니다.</div>
    <div class="alt"><a href="{{ url_for('login') }}">로그인하러 가기</a></div>
  {% else %}
    <p class="sub">{{ '새 계정을 만듭니다.' if mode=='register' else '계정으로 로그인하세요.' }}</p>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="post">
      <label>아이디</label>
      <input name="username" autocomplete="username" autocapitalize="off" required>
      <label>비밀번호</label>
      <input name="password" type="password"
        autocomplete="{{ 'new-password' if mode=='register' else 'current-password' }}" required>
      {% if mode=='register' %}
        <label>비밀번호 확인</label>
        <input name="password2" type="password" autocomplete="new-password" required>
      {% endif %}
      <button type="submit">{{ '가입하기' if mode=='register' else '로그인' }}</button>
    </form>
    {% if mode=='register' %}
      <div class="alt">이미 계정이 있으신가요? <a href="{{ url_for('login') }}">로그인</a></div>
    {% else %}
      <div class="alt">계정이 없으신가요? <a href="{{ url_for('register') }}">가입 신청</a></div>
    {% endif %}
  {% endif %}
</div></body></html>"""
ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0d0f12">
<title>관리자 · 마켓중심</title>
<style>
  :root{--bg:#0d0f12;--scr:#15171c;--s1:#1e2128;--bd:rgba(255,255,255,.09);
    --tp:#e9eaee;--ts:#a1a4ac;--tm:#6c7079;--suc:#37c07d;--up:#ff6b66;--dn:#4c8dff;--warn:#e3b341;}
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent;}
  body{margin:0;background:var(--bg);color:var(--tp);
    font-family:-apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕",sans-serif;}
  .wrap{max-width:820px;margin:0 auto;padding:16px 16px 60px;}
  .hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;}
  .hd h1{font-size:18px;font-weight:700;margin:0;}
  .hd a{font-size:12.5px;color:var(--dn);text-decoration:none;}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;margin-bottom:18px;}
  .tile{background:var(--scr);border:0.5px solid var(--bd);border-radius:12px;padding:12px 14px;}
  .tile .n{font-size:23px;font-weight:700;}
  .tile .l{font-size:11.5px;color:var(--tm);margin-top:2px;}
  h2{font-size:13.5px;color:var(--ts);margin:20px 0 8px;font-weight:600;}
  table{width:100%;border-collapse:collapse;font-size:12.5px;}
  th,td{text-align:left;padding:7px 8px;border-bottom:0.5px solid var(--bd);white-space:nowrap;}
  th{color:var(--tm);font-weight:600;}
  .tblwrap{background:var(--scr);border:0.5px solid var(--bd);border-radius:12px;overflow-x:auto;}
  .badge{font-size:10.5px;font-weight:600;padding:1px 7px;border-radius:20px;}
  .b-owner{color:var(--up);border:0.5px solid var(--up);}
  .b-admin{color:var(--warn);border:0.5px solid var(--warn);}
  .b-user{color:var(--tm);border:0.5px solid var(--tm);}
  .b-active{color:var(--suc);border:0.5px solid var(--suc);}
  .b-pending{color:var(--warn);border:0.5px solid var(--warn);}
  .b-disabled{color:var(--tm);border:0.5px solid var(--tm);}
  .act{display:flex;gap:5px;flex-wrap:wrap;}
  .act button{font-size:11px;padding:4px 8px;border:0.5px solid var(--bd);border-radius:7px;
    background:var(--s1);color:var(--tp);cursor:pointer;font-family:inherit;}
  .act button.p{border-color:var(--suc);color:var(--suc);}
  .act button.d{border-color:var(--up);color:var(--up);}
  .bar{height:7px;background:var(--dn);border-radius:4px;min-width:2px;}
  .muted{color:var(--tm);}
  .fail{color:var(--up);} .ok{color:var(--suc);}
</style></head><body>
<div class="wrap">
  <div class="hd">
    <h1>관리자 대시보드</h1>
    <div><a href="/">← 대시보드</a> &nbsp; <a href="/logout">로그아웃</a></div>
  </div>
  <div class="tiles" id="tiles"></div>

  <h2>가입 승인 대기 · 사용자 관리</h2>
  <div class="tblwrap"><table id="users"><thead><tr>
    <th>ID</th><th>아이디</th><th>역할</th><th>상태</th><th>가입</th><th>최근접속</th><th>관리</th>
  </tr></thead><tbody></tbody></table></div>

  <h2>일별 방문 (조회수 / 순방문자)</h2>
  <div class="tblwrap"><table id="daily"><thead><tr>
    <th>날짜</th><th>조회수</th><th>순방문자</th><th style="width:45%">　</th>
  </tr></thead><tbody></tbody></table></div>

  <h2>사용자별 조회수</h2>
  <div class="tblwrap"><table id="peruser"><thead><tr>
    <th>사용자</th><th>조회수</th><th>마지막 접속</th>
  </tr></thead><tbody></tbody></table></div>

  <h2>최근 로그인 기록</h2>
  <div class="tblwrap"><table id="logins"><thead><tr>
    <th>시각</th><th>아이디</th><th>IP</th><th>결과</th>
  </tr></thead><tbody></tbody></table></div>
</div>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function load(){
  const r=await fetch('/api/admin/overview'); const d=await r.json();
  const c=d.counts, s=d.stats;
  document.getElementById('tiles').innerHTML=[
    ['오늘 조회수',s.today_views],['오늘 순방문자',s.today_visitors],
    ['누적 조회수',s.total_views],['전체 계정',c.total],
    ['승인 대기',c.pending],['봇 상태',d.bot_alive?'가동중':'중지']
  ].map(t=>`<div class="tile"><div class="n">${esc(t[1])}</div><div class="l">${t[0]}</div></div>`).join('');

  document.querySelector('#users tbody').innerHTML=d.users.map(u=>{
    const rb=`<span class="badge b-${u.role}">${u.role}</span>`;
    const sb=`<span class="badge b-${u.status}">${u.status}</span>`;
    let a=[];
    if(u.role==='owner'){                 // owner 는 웹에서 변경 불가(CLI 전용)
      a.push('<span class="muted">—</span>');
    }else{
      if(u.status==='pending')a.push(`<button class="p" onclick="act(${u.id},'approve')">승인</button>`);
      if(u.status==='active')a.push(`<button onclick="act(${u.id},'disable')">비활성</button>`);
      if(u.status==='disabled')a.push(`<button class="p" onclick="act(${u.id},'approve')">복구</button>`);
      a.push(u.role==='admin'
        ?`<button onclick="act(${u.id},'make-user')">관리자해제</button>`
        :`<button onclick="act(${u.id},'make-admin')">관리자지정</button>`);
      a.push(`<button class="d" onclick="del(${u.id},'${esc(u.username)}')">삭제</button>`);
    }
    return `<tr><td>${u.id}</td><td>${esc(u.username)}</td><td>${rb}</td><td>${sb}</td>
      <td class="muted">${esc((u.created_at||'').slice(0,10))}</td>
      <td class="muted">${esc(u.last_login||'-')}</td>
      <td><div class="act">${a.join('')}</div></td></tr>`;
  }).join('');

  const maxV=Math.max(1,...s.daily.map(x=>x.views));
  document.querySelector('#daily tbody').innerHTML=s.daily.map(x=>
    `<tr><td>${esc(x.day)}</td><td>${x.views}</td><td>${x.visitors}</td>
     <td><div class="bar" style="width:${Math.round(x.views/maxV*100)}%"></div></td></tr>`).join('')
    ||'<tr><td colspan="4" class="muted">기록 없음</td></tr>';

  document.querySelector('#peruser tbody').innerHTML=s.per_user.map(x=>
    `<tr><td>${esc(x.username)}</td><td>${x.views}</td><td class="muted">${esc(x.last_seen||'-')}</td></tr>`).join('')
    ||'<tr><td colspan="3" class="muted">기록 없음</td></tr>';

  document.querySelector('#logins tbody').innerHTML=s.logins.map(x=>
    `<tr><td class="muted">${esc(x.ts)}</td><td>${esc(x.username)}</td><td class="muted">${esc(x.ip)}</td>
     <td class="${x.ok?'ok':'fail'}">${x.ok?'성공':'실패'}</td></tr>`).join('')
    ||'<tr><td colspan="4" class="muted">기록 없음</td></tr>';
}
async function act(id,action){
  const fd=new FormData(); fd.append('action',action);
  const r=await fetch('/api/admin/user/'+id,{method:'POST',body:fd});
  const j=await r.json(); if(j.detail)alert(j.detail); load();
}
function del(id,name){ if(confirm(`'${name}' 계정을 삭제할까요?`)) act(id,'delete'); }
load();
</script></body></html>"""


if __name__ == "__main__":
    # HTTPS 도입 후에는 nginx(80/443) → Flask(127.0.0.1:8000) 구조.
    # 127.0.0.1 바인딩이라 외부에서 8000 포트로 nginx 를 우회해 직접 접속할 수 없다.
    # 필요 시 config.py 의 PORT / HOST 로 덮어쓸 수 있다.
    host, port = "127.0.0.1", 8000
    try:
        import config
        port = int(getattr(config, "PORT", port))
        host = str(getattr(config, "HOST", host))
    except Exception:
        pass
    app.run(host=host, port=port)
