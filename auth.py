"""
인증 / 사용자 DB 모듈 (SQLite, 표준 라이브러리 + werkzeug 해시).
- users     : 계정 (역할 user/admin, 상태 pending/active/disabled)
- pageviews : 방문/조회 로그 (통계용)
- login_log : 로그인 시도 기록

CLI:
  python3 auth.py create-admin <아이디> <비밀번호>   # 최초 관리자 시딩
  python3 auth.py set-owner <아이디>                  # 계정을 owner(공지 작성 권한)로
  python3 auth.py list                                # 계정 목록
  python3 auth.py set-pw <아이디> <새비밀번호>        # 비밀번호 재설정
"""
import os
import sys
import sqlite3
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "users.db")

# 신규 가입 기본 상태. 'pending'=관리자 승인 필요, 'active'=자동 승인.
DEFAULT_SIGNUP_STATUS = "active"

# 역할: user < admin < owner.  owner 는 관리자 권한 + 공지 작성 권한.
ROLES = ("user", "admin", "owner")


def _now() -> str:
    # 서버 시간 = KST
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            username   TEXT UNIQUE NOT NULL,
            pw_hash    TEXT NOT NULL,
            role       TEXT NOT NULL DEFAULT 'user',
            status     TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            last_login TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS pageviews(
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER,
            username TEXT,
            path     TEXT,
            ip       TEXT,
            ua       TEXT,
            ts       TEXT NOT NULL,
            day      TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS login_log(
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            ip       TEXT,
            ua       TEXT,
            ok       INTEGER,
            ts       TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pv_day ON pageviews(day)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pv_user ON pageviews(user_id)")


# ── 계정 ────────────────────────────────────────────────
import re as _re
_USERNAME_RE = _re.compile(r"^[A-Za-z0-9가-힣._-]{3,20}$")


def create_user(username, password, role="user", status=None):
    username = (username or "").strip()
    if not username or not password:
        return None, "아이디와 비밀번호를 입력하세요."
    if not _USERNAME_RE.match(username):
        return None, "아이디는 3~20자의 한글/영문/숫자/._- 만 쓸 수 있습니다."
    if len(password) < 8:
        return None, "비밀번호는 8자 이상이어야 합니다."
    if status is None:
        status = DEFAULT_SIGNUP_STATUS
    try:
        with _conn() as c:
            cur = c.execute(
                "INSERT INTO users(username, pw_hash, role, status, created_at) "
                "VALUES(?,?,?,?,?)",
                (username, generate_password_hash(password), role, status, _now()),
            )
            return cur.lastrowid, None
    except sqlite3.IntegrityError:
        return None, "이미 존재하는 아이디입니다."


def get_user_by_name(username):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None


def get_user_by_id(uid):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None


def verify_login(username, password):
    """(user_dict, None) 또는 (None, 에러메시지)."""
    u = get_user_by_name((username or "").strip())
    if not u or not check_password_hash(u["pw_hash"], password or ""):
        return None, "아이디 또는 비밀번호가 올바르지 않습니다."
    if u["status"] == "pending":
        return None, "가입 승인 대기 중입니다. 관리자 승인 후 이용할 수 있습니다."
    if u["status"] == "disabled":
        return None, "비활성화된 계정입니다."
    with _conn() as c:
        c.execute("UPDATE users SET last_login=? WHERE id=?", (_now(), u["id"]))
    u["last_login"] = _now()
    return u, None


def set_password(username, password):
    with _conn() as c:
        cur = c.execute("UPDATE users SET pw_hash=? WHERE username=?",
                        (generate_password_hash(password), username))
        return cur.rowcount > 0


def list_users():
    with _conn() as c:
        rows = c.execute(
            "SELECT id, username, role, status, created_at, last_login "
            "FROM users ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]


def count_users():
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        pending = c.execute("SELECT COUNT(*) FROM users WHERE status='pending'").fetchone()[0]
        active = c.execute("SELECT COUNT(*) FROM users WHERE status='active'").fetchone()[0]
        return {"total": total, "pending": pending, "active": active}


def set_status(uid, status):
    if status not in ("pending", "active", "disabled"):
        return False
    with _conn() as c:
        cur = c.execute("UPDATE users SET status=? WHERE id=?", (status, uid))
        return cur.rowcount > 0


def set_role(uid, role):
    if role not in ROLES:
        return False
    with _conn() as c:
        cur = c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
        return cur.rowcount > 0


def is_admin(u) -> bool:
    """관리자 권한(admin 또는 owner) 보유 여부."""
    return bool(u) and u.get("role") in ("admin", "owner")


def is_owner(u) -> bool:
    return bool(u) and u.get("role") == "owner"


def delete_user(uid):
    with _conn() as c:
        cur = c.execute("DELETE FROM users WHERE id=?", (uid,))
        return cur.rowcount > 0


# ── 로깅 ────────────────────────────────────────────────
def log_pageview(user, path, ip, ua):
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO pageviews(user_id, username, path, ip, ua, ts, day) "
                "VALUES(?,?,?,?,?,?,?)",
                (user.get("id") if user else None,
                 user.get("username") if user else None,
                 path, ip, (ua or "")[:300], _now(), _today()),
            )
    except Exception:
        pass  # 통계 실패가 페이지를 막지 않도록


def log_login(username, ip, ua, ok):
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO login_log(username, ip, ua, ok, ts) VALUES(?,?,?,?,?)",
                (username, ip, (ua or "")[:300], 1 if ok else 0, _now()),
            )
    except Exception:
        pass


# ── 통계 ────────────────────────────────────────────────
def stats(days=14):
    """관리자 대시보드용 집계."""
    with _conn() as c:
        total_views = c.execute("SELECT COUNT(*) FROM pageviews").fetchone()[0]
        today_views = c.execute("SELECT COUNT(*) FROM pageviews WHERE day=?",
                                (_today(),)).fetchone()[0]
        today_visitors = c.execute(
            "SELECT COUNT(DISTINCT COALESCE(user_id, ip)) FROM pageviews WHERE day=?",
            (_today(),)).fetchone()[0]
        # 일별 (조회수 / 순방문자)
        daily = c.execute(
            "SELECT day, COUNT(*) AS views, "
            "COUNT(DISTINCT COALESCE(user_id, ip)) AS visitors "
            "FROM pageviews GROUP BY day ORDER BY day DESC LIMIT ?",
            (days,)).fetchall()
        # 유저별 조회수
        per_user = c.execute(
            "SELECT COALESCE(username,'(비로그인)') AS username, COUNT(*) AS views, "
            "MAX(ts) AS last_seen "
            "FROM pageviews GROUP BY username ORDER BY views DESC LIMIT 50"
        ).fetchall()
        # 최근 로그인 기록
        logins = c.execute(
            "SELECT username, ip, ok, ts FROM login_log ORDER BY id DESC LIMIT 40"
        ).fetchall()
    return {
        "total_views": total_views,
        "today_views": today_views,
        "today_visitors": today_visitors,
        "daily": [dict(r) for r in daily],
        "per_user": [dict(r) for r in per_user],
        "logins": [dict(r) for r in logins],
    }


# ── CLI (최초 관리자 시딩 등) ─────────────────────────────
def _cli():
    init_db()
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    if cmd == "create-admin" and len(sys.argv) >= 4:
        uid, err = create_user(sys.argv[2], sys.argv[3], role="admin", status="active")
        print("에러:", err) if err else print(f"관리자 생성 완료 (id={uid}, {sys.argv[2]})")
    elif cmd == "set-owner" and len(sys.argv) >= 3:
        u = get_user_by_name(sys.argv[2])
        if not u:
            print("해당 아이디 없음:", sys.argv[2])
        else:
            set_role(u["id"], "owner")
            set_status(u["id"], "active")
            print(f"owner 로 설정 완료 ({sys.argv[2]}) — 공지 작성 권한 부여")
    elif cmd == "set-pw" and len(sys.argv) >= 4:
        print("변경 완료" if set_password(sys.argv[2], sys.argv[3]) else "해당 아이디 없음")
    elif cmd == "list":
        for u in list_users():
            print(f"[{u['id']}] {u['username']:16} {u['role']:6} {u['status']:9} "
                  f"가입 {u['created_at']}  최근 {u['last_login'] or '-'}")
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
