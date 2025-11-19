# main.py
import os
import uuid
import random
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from contextlib import contextmanager

# =========================
# إعدادات عامة
# =========================
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/plinko")

INITIAL_BALANCE = 5.0
MIN_BET = 1.0
MAX_BET = 5.0

MULTIPLIER_DISTRIBUTION = [
    {"m": 0,    "p": 0.6516},
    {"m": 1,    "p": 0.18},
    {"m": 2,    "p": 0.14},
    {"m": 5,    "p": 0.015},
    {"m": 10,   "p": 0.008},
    {"m": 25,   "p": 0.004},
    {"m": 50,   "p": 0.001},
    {"m": 100,  "p": 0.0003},
    {"m": 1000, "p": 0.0001},
]

# =========================
# اتصال بالداتابيس (Connection Pool)
# =========================
pool: SimpleConnectionPool | None = None

def init_pool():
    global pool
    if pool is None:
        pool = SimpleConnectionPool(1, 20, DATABASE_URL)

@contextmanager
def get_db():
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)

# =========================
# FastAPI
# =========================
app = FastAPI(title="Racetan Plinko")

# لو حبيت من دومين مختلف، تقدر تخصص allow_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # بما إننا هنستخدم نفس السيرفر، ما في مشكلة
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def on_startup():
    init_pool()

@app.on_event("shutdown")
def on_shutdown():
    if pool:
        pool.closeall()

# =========================
# Models
# =========================
class LoginIn(BaseModel):
    username: str
    password: str

class LoginOut(BaseModel):
    ok: bool
    session_token: Optional[str] = None
    error: Optional[str] = None

class InitSessionIn(BaseModel):
    session_token: str

class InitSessionOut(BaseModel):
    ok: bool
    balance: float
    balls_played: int
    cashed_out: bool
    finished: bool

class DropIn(BaseModel):
    session_token: str
    stake: float

class DropOut(BaseModel):
    ok: bool
    balance: float
    balls_played: int
    cashed_out: bool
    finished: bool
    multiplier: int
    stake: float

class CashoutIn(BaseModel):
    session_token: str

class CashoutOut(BaseModel):
    ok: bool
    balance: float
    cashed_out: bool
    finished: bool

# =========================
# Helpers
# =========================
def pick_multiplier() -> int:
    r = random.random()
    acc = 0.0
    for item in MULTIPLIER_DISTRIBUTION:
        acc += item["p"]
        if r <= acc:
            return item["m"]
    return MULTIPLIER_DISTRIBUTION[-1]["m"]

def get_user_by_session(conn, session_token: str):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT u.*
            FROM auth_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.session_token = %s
              AND u.is_active = TRUE
              AND (s.expires_at IS NULL OR s.expires_at > NOW())
            LIMIT 1;
        """, (session_token,))
        return cur.fetchone()

def get_game_session_for_update(conn, user_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM game_sessions
            WHERE user_id = %s
            FOR UPDATE;
        """, (user_id,))
        return cur.fetchone()

# =========================
# API: Login
# =========================
@app.post("/api/login", response_model=LoginOut)
def login(body: LoginIn):
    username = body.username.strip()
    password = body.password.strip()

    if not username or not password:
        return LoginOut(ok=False, error="الرجاء إدخال اسم المستخدم وكلمة السر")

    with get_db() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE username = %s AND is_active = TRUE;", (username,))
        user = cur.fetchone()
        if not user:
            return LoginOut(ok=False, error="المستخدم غير موجود أو غير مفعل")

        # التحقق من كلمة السر
        cur.execute("SELECT crypt(%s, %s) = %s AS ok;",
                    (password, user["password_hash"], user["password_hash"]))
        pass_ok = cur.fetchone()["ok"]
        if not pass_ok:
            return LoginOut(ok=False, error="كلمة السر غير صحيحة")

        # نلغي الجلسات القديمة لنفس المستخدم (جلسة واحدة فقط)
        cur.execute("DELETE FROM auth_sessions WHERE user_id = %s;", (user["id"],))

        token = str(uuid.uuid4())
        cur.execute("""
            INSERT INTO auth_sessions (user_id, session_token, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '24 hours');
        """, (user["id"], token))
        conn.commit()

    return LoginOut(ok=True, session_token=token)

# =========================
# API: init-session
# =========================
@app.post("/api/init-session", response_model=InitSessionOut)
def init_session(body: InitSessionIn):
    session_token = body.session_token.strip()
    if not session_token:
        raise HTTPException(status_code=400, detail="session_token مطلوب")

    with get_db() as conn:
        user = get_user_by_session(conn, session_token)
        if not user:
            raise HTTPException(status_code=401, detail="جلسة غير صالحة أو منتهية")

        with conn.cursor() as cur:
            cur.execute("SELECT * FROM game_sessions WHERE user_id = %s;", (user["id"],))
            gs = cur.fetchone()

            if gs:
                return InitSessionOut(
                    ok=True,
                    balance=float(gs["balance"]),
                    balls_played=gs["balls_played"],
                    cashed_out=gs["cashed_out"],
                    finished=gs["finished"],
                )

            # إنشاء جلسة جديدة للعبة (مرة واحدة)
            cur.execute("""
                INSERT INTO game_sessions (user_id, session_token, balance)
                VALUES (%s, %s, %s)
                RETURNING *;
            """, (user["id"], session_token, INITIAL_BALANCE))
            new_gs = cur.fetchone()
            conn.commit()

    return InitSessionOut(
        ok=True,
        balance=float(new_gs["balance"]),
        balls_played=new_gs["balls_played"],
        cashed_out=new_gs["cashed_out"],
        finished=new_gs["finished"],
    )

# =========================
# API: drop (إسقاط كرة)
# =========================
@app.post("/api/drop", response_model=DropOut)
def drop_ball(body: DropIn):
    session_token = body.session_token.strip()
    stake = max(MIN_BET, min(MAX_BET, float(body.stake)))

    with get_db() as conn:
        user = get_user_by_session(conn, session_token)
        if not user:
            raise HTTPException(status_code=401, detail="جلسة غير صالحة")

        session = get_game_session_for_update(conn, user["id"])
        if not session:
            raise HTTPException(status_code=400, detail="لا توجد جلسة لعبة لهذا المستخدم")

        if session["finished"] or session["cashed_out"]:
            raise HTTPException(status_code=400, detail="الجلسة منتهية أو تم الكاش آوت")

        balance = float(session["balance"])
        if balance < stake:
            raise HTTPException(status_code=400, detail="الرصيد لا يكفي")

        balance -= stake
        multiplier = pick_multiplier()
        win_amount = stake * multiplier
        balance += win_amount
        balls_played = session["balls_played"] + 1

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE game_sessions
                SET balance = %s,
                    balls_played = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *;
            """, (balance, balls_played, session["id"]))
            updated = cur.fetchone()

            cur.execute("""
                INSERT INTO game_rounds (session_id, stake, multiplier, win_amount)
                VALUES (%s, %s, %s, %s);
            """, (session["id"], stake, multiplier, win_amount))

            conn.commit()

    return DropOut(
        ok=True,
        balance=float(updated["balance"]),
        balls_played=updated["balls_played"],
        cashed_out=updated["cashed_out"],
        finished=updated["finished"],
        multiplier=multiplier,
        stake=stake,
    )

# =========================
# API: cashout
# =========================
@app.post("/api/cashout", response_model=CashoutOut)
def cashout(body: CashoutIn):
    session_token = body.session_token.strip()

    with get_db() as conn:
        user = get_user_by_session(conn, session_token)
        if not user:
            raise HTTPException(status_code=401, detail="جلسة غير صالحة")

        session = get_game_session_for_update(conn, user["id"])
        if not session:
            raise HTTPException(status_code=400, detail="لا توجد جلسة لعبة")

        if session["cashed_out"]:
            return CashoutOut(
                ok=True,
                balance=float(session["balance"]),
                cashed_out=True,
                finished=session["finished"],
            )

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE game_sessions
                SET cashed_out = TRUE,
                    finished = TRUE,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING *;
            """, (session["id"],))
            updated = cur.fetchone()
            conn.commit()

    return CashoutOut(
        ok=True,
        balance=float(updated["balance"]),
        cashed_out=True,
        finished=True,
    )

# =========================
# Static Files (frontend)
# =========================
# نخلي index.html هي الصفحة الرئيسية (login)
app.mount("/", StaticFiles(directory="frontend", html=True), name="static")
