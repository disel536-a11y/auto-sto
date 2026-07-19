"""
공지 & 문의 게시판 모듈 (SQLite, 표준 라이브러리).
auth.py 의 users.db 와 같은 파일을 쓴다(테이블만 추가).

- notices  : 공지사항.   작성/수정/삭제 = owner 전용.
- posts    : 문의/개선.  작성 = 로그인 유저 누구나. 목록은 전체 공개.
- comments : 문의 댓글.  작성 = admin/owner 전용.

권한 판정은 app.py 라우트에서 하고, 여기서는 순수 CRUD 만 담당한다.
"""
import os
import sqlite3
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "users.db")          # auth.py 와 동일 파일


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS notices(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            body       TEXT NOT NULL,
            author     TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS posts(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            username   TEXT,
            title      TEXT NOT NULL,
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS comments(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id    INTEGER NOT NULL,
            user_id    INTEGER,
            username   TEXT,
            role       TEXT,
            body       TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cmt_post ON comments(post_id)")


# ── 유효성 ───────────────────────────────────────────────
def _clean(title, body, tmax=200, bmax=5000):
    title = (title or "").strip()
    body = (body or "").strip()
    if not title or not body:
        return None, None, "제목과 내용을 모두 입력하세요."
    return title[:tmax], body[:bmax], None


# ── 공지 ─────────────────────────────────────────────────
def list_notices():
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, body, author, created_at, updated_at "
            "FROM notices ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def create_notice(title, body, author):
    title, body, err = _clean(title, body)
    if err:
        return None, err
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO notices(title, body, author, created_at) VALUES(?,?,?,?)",
            (title, body, author, _now()))
        return cur.lastrowid, None


def update_notice(nid, title, body):
    title, body, err = _clean(title, body)
    if err:
        return err
    with _conn() as c:
        c.execute("UPDATE notices SET title=?, body=?, updated_at=? WHERE id=?",
                  (title, body, _now(), nid))
    return None


def delete_notice(nid):
    with _conn() as c:
        cur = c.execute("DELETE FROM notices WHERE id=?", (nid,))
        return cur.rowcount > 0


# ── 문의/개선 ────────────────────────────────────────────
def list_posts():
    """문의 목록 + 각 글의 댓글을 함께 반환(소규모라 인라인으로 충분)."""
    with _conn() as c:
        posts = [dict(r) for r in c.execute(
            "SELECT id, user_id, username, title, body, created_at "
            "FROM posts ORDER BY id DESC").fetchall()]
        cmts = [dict(r) for r in c.execute(
            "SELECT id, post_id, username, role, body, created_at "
            "FROM comments ORDER BY id ASC").fetchall()]
    by_post = {}
    for cm in cmts:
        by_post.setdefault(cm["post_id"], []).append(cm)
    for p in posts:
        p["comments"] = by_post.get(p["id"], [])
    return posts


def get_post(pid):
    with _conn() as c:
        r = c.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
        return dict(r) if r else None


def create_post(user, title, body):
    title, body, err = _clean(title, body)
    if err:
        return None, err
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO posts(user_id, username, title, body, created_at) VALUES(?,?,?,?,?)",
            (user.get("id"), user.get("username"), title, body, _now()))
        return cur.lastrowid, None


def delete_post(pid):
    with _conn() as c:
        c.execute("DELETE FROM comments WHERE post_id=?", (pid,))   # 댓글도 함께 정리
        cur = c.execute("DELETE FROM posts WHERE id=?", (pid,))
        return cur.rowcount > 0


# ── 댓글 ─────────────────────────────────────────────────
def get_comment(cid):
    with _conn() as c:
        r = c.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None


def add_comment(pid, user, body):
    body = (body or "").strip()
    if not body:
        return None, "내용을 입력하세요."
    if not get_post(pid):
        return None, "삭제되었거나 없는 글입니다."
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO comments(post_id, user_id, username, role, body, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (pid, user.get("id"), user.get("username"), user.get("role"),
             body[:3000], _now()))
        return cur.lastrowid, None


def delete_comment(cid):
    with _conn() as c:
        cur = c.execute("DELETE FROM comments WHERE id=?", (cid,))
        return cur.rowcount > 0


if __name__ == "__main__":
    init_db()
    print("board 테이블 초기화 완료:", DB)
