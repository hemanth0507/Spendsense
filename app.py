# app.py - SpendSense (full monolithic file)
# Run: streamlit run app.py

import streamlit as st
import sqlite3
import uuid
import hashlib
import time
import difflib
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from dateutil import tz
import os
from PIL import Image
from io import BytesIO
from email.message import EmailMessage
import smtplib

# ----------------------------
# Config / constants
# ----------------------------
APP_NAME = "SpendSense"
DB_FILE = "spendsense.db"
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

BADGES = [
    (1000, "Impulse Rookie"),
    (5000, "Mindful Saver"),
    (20000, "Budget Pro"),
    (50000, "Frugal Master"),
]

st.set_page_config(page_title=f"{APP_NAME} – Social Decision App", page_icon="🛑🛒", layout="wide")

# ----------------------------
# DB helpers & init
# ----------------------------
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # users
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    # groups
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            name TEXT NOT NULL,
            invite_code TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(owner_id) REFERENCES users(id)
        );
        """
    )

    # memberships
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS memberships (
            user_id TEXT NOT NULL,
            group_id TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            PRIMARY KEY (user_id, group_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(group_id) REFERENCES groups(id)
        );
        """
    )

    # posts
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            item_name TEXT NOT NULL,
            item_link TEXT,
            price REAL NOT NULL,
            reason TEXT,
            image_path TEXT,
            deadline_utc TEXT NOT NULL,
            status TEXT NOT NULL, -- pending, closed, decided
            decision TEXT,        -- bought, skipped
            decided_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(group_id) REFERENCES groups(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # votes
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS votes (
            id TEXT PRIMARY KEY,
            post_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            vote TEXT NOT NULL, -- buy, dont_buy, neutral
            comment TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(post_id, user_id),
            FOREIGN KEY(post_id) REFERENCES posts(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    # notifications
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            group_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            type TEXT NOT NULL, -- new_post, closing_soon, closed
            created_at TEXT NOT NULL
        );
        """
    )

    # savings
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS savings (
            user_id TEXT PRIMARY KEY,
            total_saved REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )

    conn.commit()
    conn.close()

# initialize DB
init_db()

# ----------------------------
# Utility functions
# ----------------------------
def now_utc_iso() -> str:
    return datetime.utcnow().isoformat()

def to_local(iso_ts: str, tz_name: str = "Asia/Kolkata") -> str:
    try:
        dt = datetime.fromisoformat(iso_ts)
        dt = dt.replace(tzinfo=tz.tzutc()).astimezone(tz.gettz(tz_name))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_ts

def hash_password(pw: str) -> str:
    salt = "spendsense_salt_v1"
    return hashlib.sha256((pw + salt).encode()).hexdigest()

# ----------------------------
# Ensure & backfill savings rows
# ----------------------------
def ensure_savings_row(user_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO savings (user_id, total_saved, updated_at) VALUES (?, 0, ?)",
        (user_id, now_utc_iso()),
    )
    conn.commit()
    conn.close()

def backfill_savings_rows():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO savings (user_id, total_saved, updated_at) "
        "SELECT id, 0, ? FROM users",
        (now_utc_iso(),),
    )
    conn.commit()
    conn.close()

# do a safe backfill on startup (idempotent)
backfill_savings_rows()

# ----------------------------
# Email sending (optional)
# ----------------------------
def send_email(recipients, subject, body):
    """
    Optional email via st.secrets SMTP settings.
    Set in .streamlit/secrets.toml or Streamlit Cloud secrets:
    [smtp]
    host = "smtp.gmail.com"
    port = 587
    user = "you@example.com"
    password = "app_password"
    from_email = "SpendSense <you@example.com>"
    """
    try:
        cfg = st.secrets["smtp"]
    except Exception:
        return False, "SMTP not configured"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg.get("from_email", cfg.get("user"))
        msg["To"] = ", ".join(recipients)
        msg.set_content(body)
        with smtplib.SMTP(cfg["host"], int(cfg.get("port", 587))) as s:
            s.starttls()
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        return True, "sent"
    except Exception as e:
        return False, str(e)

# ----------------------------
# Data access & logic
# ----------------------------
def create_user(email, name, password):
    conn = get_conn()
    cur = conn.cursor()
    uid = str(uuid.uuid4())
    try:
        cur.execute(
            "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (uid, email.lower().strip(), name.strip(), hash_password(password), now_utc_iso()),
        )
        conn.commit()
        # ensure savings row exists
        cur.execute(
            "INSERT OR IGNORE INTO savings (user_id, total_saved, updated_at) VALUES (?, ?, ?)",
            (uid, 0.0, now_utc_iso()),
        )
        conn.commit()
        return uid
    except sqlite3.IntegrityError:
        return None
    finally:
        conn.close()

def auth_user(email, password):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    if row["password_hash"] == hash_password(password):
        return dict(row)
    return None

def get_user(uid):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE id=?", (uid,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def create_group(owner_id, name, short_code=True):
    conn = get_conn()
    cur = conn.cursor()
    gid = str(uuid.uuid4())
    invite = str(int(time.time()))[-6:] if short_code else uuid.uuid4().hex[:8].upper()
    cur.execute(
        "INSERT INTO groups (id, owner_id, name, invite_code, created_at) VALUES (?, ?, ?, ?, ?)",
        (gid, owner_id, name.strip(), invite, now_utc_iso()),
    )
    cur.execute(
        "INSERT OR IGNORE INTO memberships (user_id, group_id, joined_at) VALUES (?, ?, ?)",
        (owner_id, gid, now_utc_iso()),
    )
    conn.commit()
    conn.close()
    return gid

def join_group(user_id, invite_code):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM groups WHERE invite_code=?", (invite_code.strip().upper(),))
    g = cur.fetchone()
    if not g:
        conn.close()
        return None, "Invalid invite code"
    try:
        cur.execute(
            "INSERT INTO memberships (user_id, group_id, joined_at) VALUES (?, ?, ?)",
            (user_id, g["id"], now_utc_iso()),
        )
        conn.commit()
        return g["id"], None
    except sqlite3.IntegrityError:
        return g["id"], None
    finally:
        conn.close()

def list_groups(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT g.* FROM groups g
        JOIN memberships m ON g.id = m.group_id
        WHERE m.user_id=?
        ORDER BY g.created_at DESC
        """,
        (user_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_group(group_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM groups WHERE id=?", (group_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def group_members(group_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT u.* FROM users u
        JOIN memberships m ON u.id = m.user_id
        WHERE m.group_id=?
        """,
        (group_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def create_post(group_id, user_id, item_name, item_link, price, reason, image_bytes, deadline_dt):
    image_path = None
    pid = str(uuid.uuid4())
    if image_bytes is not None:
        image_path = os.path.join(UPLOAD_DIR, f"{pid}.jpg")
        try:
            img = Image.open(BytesIO(image_bytes))
            img = img.convert("RGB")
            img.save(image_path, format="JPEG", quality=85)
        except Exception:
            image_path = None

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO posts (id, group_id, user_id, item_name, item_link, price, reason, image_path, deadline_utc, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            pid,
            group_id,
            user_id,
            item_name.strip(),
            (item_link or '').strip(),
            float(price),
            (reason or '').strip(),
            image_path,
            deadline_dt.astimezone(tz.tzutc()).replace(tzinfo=None).isoformat(),
            now_utc_iso(),
        ),
    )
    cur.execute(
        "INSERT INTO notifications (id, group_id, post_id, type, created_at) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), group_id, pid, 'new_post', now_utc_iso()),
    )
    conn.commit()
    conn.close()
    return pid

def list_posts(group_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM posts WHERE group_id=? ORDER BY created_at DESC",
        (group_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_post(post_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM posts WHERE id=?", (post_id,))
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def cast_vote(post_id, user_id, vote, comment):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO votes (id, post_id, user_id, vote, comment, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), post_id, user_id, vote, (comment or '').strip(), now_utc_iso()),
        )
    except sqlite3.IntegrityError:
        cur.execute(
            "UPDATE votes SET vote=?, comment=?, created_at=? WHERE post_id=? AND user_id=?",
            (vote, (comment or '').strip(), now_utc_iso(), post_id, user_id),
        )
    conn.commit()
    conn.close()

def post_votes(post_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT v.*, u.name FROM votes v
        JOIN users u ON v.user_id = u.id
        WHERE v.post_id=? ORDER BY v.created_at DESC
        """,
        (post_id,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def vote_counts(post_id):
    votes = post_votes(post_id)
    counts = {"buy": 0, "dont_buy": 0, "neutral": 0}
    for v in votes:
        if v["vote"] in counts:
            counts[v["vote"]] += 1
    return counts, votes

def close_if_due(post):
    if post["status"] != "pending":
        return False
    try:
        deadline = datetime.fromisoformat(post["deadline_utc"]).replace(tzinfo=tz.tzutc())
    except Exception:
        return False
    if datetime.utcnow().replace(tzinfo=tz.tzutc()) >= deadline:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE posts SET status='closed' WHERE id=?", (post["id"],))
        cur.execute(
            "INSERT INTO notifications (id, group_id, post_id, type, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), post["group_id"], post["id"], 'closed', now_utc_iso()),
        )
        conn.commit()
        conn.close()
        return True
    return False

def decide_post(post_id, decision):
    """
    decision: 'bought' or 'skipped'
    If skipped, increments savings for the post owner by the post price.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE posts SET status='decided', decision=?, decided_at=? WHERE id=?",
        (decision, now_utc_iso(), post_id),
    )
    cur.execute("SELECT user_id, price FROM posts WHERE id=?", (post_id,))
    r = cur.fetchone()
    if r and decision == 'skipped':
        owner_id, price = r[0], float(r[1])
        cur.execute("SELECT total_saved FROM savings WHERE user_id=?", (owner_id,))
        s = cur.fetchone()
        current = float(s[0]) if s else 0.0
        new_total = current + price
        cur.execute(
            "INSERT OR REPLACE INTO savings (user_id, total_saved, updated_at) VALUES (?, ?, ?)",
            (owner_id, new_total, now_utc_iso()),
        )
    conn.commit()
    conn.close()

def get_savings(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT total_saved FROM savings WHERE user_id=?", (user_id,))
    r = cur.fetchone()
    conn.close()
    return float(r[0]) if r else 0.0

def badge_for(total_saved: float):
    earned = None
    for thr, name in BADGES:
        if total_saved >= thr:
            earned = name
    return earned

def notify_group_new_post(group_id, post, creator_name):
    members = group_members(group_id)
    emails = [m["email"] for m in members if m.get("email")]
    if not emails:
        return
    subject = f"{APP_NAME}: {creator_name} posted – {post['item_name']}"
    link = post.get("item_link") or "(no link provided)"
    try:
        deadline = to_local(post["deadline_utc"]) + " IST"
    except Exception:
        deadline = "(deadline unknown)"
    body = f"New item posted in your group.\n\nItem: {post['item_name']}\nPrice: {post['price']}\nReason: {post['reason']}\nLink: {link}\nVote before: {deadline}\n\nOpen the app to vote."
    send_email(emails, subject, body)

# ----------------------------
# Auto-suggestion
# ----------------------------
def check_purchase_history(user_id, item_name, threshold=0.6):
    """
    Check user's past posts (all posts) and suggest if similar item exists.
    Uses difflib.get_close_matches first, then falls back to SequenceMatcher ratio.
    """
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("SELECT item_name FROM posts WHERE user_id=?", (user_id,))
        rows = cur.fetchall()
    except Exception:
        rows = []
    conn.close()

    past_items = [(r[0] or "") for r in rows]

    if not past_items:
        return "✅ Suggestion: No past items found — this might be a new need."

    # difflib quick match
    try:
        names = [p.lower() for p in past_items]
        matches = difflib.get_close_matches(item_name.lower(), names, n=1, cutoff=threshold)
        if matches:
            matched = matches[0]
            orig = next((p for p in past_items if p.lower() == matched), matched)
            return f"⚠️ Suggestion: You already posted something similar earlier: '{orig}'. Consider skipping."
    except Exception:
        pass

    # fallback SequenceMatcher
    for past in past_items:
        try:
            ratio = SequenceMatcher(None, past.lower(), item_name.lower()).ratio()
            if ratio >= threshold:
                return f"⚠️ Suggestion: You already posted something similar earlier: '{past}'. Consider skipping."
        except Exception:
            continue

    return "✅ Suggestion: No close match in your past posts — you might need this."

# ----------------------------
# Delete helpers
# ----------------------------
def delete_post(post_id, user_id):
    """Delete a post (only if owner and pending or decided — we allow deletion only if pending for safety)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT user_id, image_path, status FROM posts WHERE id=?", (post_id,))
    r = cur.fetchone()
    if not r or r["user_id"] != user_id:
        conn.close()
        return False
    if r["status"] != "pending":
        # allow deletion only for pending posts (you can adjust policy)
        conn.close()
        return False
    image_path = r["image_path"]
    cur.execute("DELETE FROM votes WHERE post_id=?", (post_id,))
    cur.execute("DELETE FROM notifications WHERE post_id=?", (post_id,))
    cur.execute("DELETE FROM posts WHERE id=?", (post_id,))
    conn.commit()
    conn.close()
    if image_path and os.path.exists(image_path):
        try:
            os.remove(image_path)
        except Exception:
            pass
    return True

def delete_group(group_id, user_id):
    """Delete a group and all related data if the user is the owner."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT owner_id FROM groups WHERE id=?", (group_id,))
    g = cur.fetchone()
    if not g or g["owner_id"] != user_id:
        conn.close()
        return False
    # remove posts, votes, notifications, memberships
    cur.execute("SELECT id, image_path FROM posts WHERE group_id=?", (group_id,))
    posts = cur.fetchall()
    for p in posts:
        pid = p["id"]
        path = p["image_path"]
        cur.execute("DELETE FROM votes WHERE post_id=?", (pid,))
        cur.execute("DELETE FROM notifications WHERE post_id=?", (pid,))
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    cur.execute("DELETE FROM posts WHERE group_id=?", (group_id,))
    cur.execute("DELETE FROM memberships WHERE group_id=?", (group_id,))
    cur.execute("DELETE FROM groups WHERE id=?", (group_id,))
    conn.commit()
    conn.close()
    return True

def clear_user_data(user_id):
    """Delete user and all their data (use with caution)."""
    conn = get_conn()
    cur = conn.cursor()
    # delete groups owned (and their related data)
    cur.execute("SELECT id FROM groups WHERE owner_id=?", (user_id,))
    groups_owned = [r["id"] for r in cur.fetchall()]
    for gid in groups_owned:
        delete_group(gid, user_id)
    # delete posts by user (and clean image files)
    cur.execute("SELECT image_path FROM posts WHERE user_id=?", (user_id,))
    for r in cur.fetchall():
        path = r["image_path"]
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
    cur.execute("DELETE FROM votes WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM posts WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM memberships WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM savings WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()

# ----------------------------
# Streamlit UI
# ----------------------------
# Initialize session keys
if "user" not in st.session_state:
    st.session_state.user = None
if "active_gid" not in st.session_state:
    st.session_state.active_gid = None
if "post_auto_skip" not in st.session_state:
    st.session_state.post_auto_skip = False

st.sidebar.title("🛑🛒 SpendSense")
auth_mode = st.sidebar.radio("Auth", ["Login", "Sign up"], horizontal=True)

# ----------------------------
# Auth forms
# ----------------------------
if st.session_state.user is None:
    with st.sidebar.form("auth_form"):
        email = st.text_input("Email")
        if auth_mode == "Sign up":
            name = st.text_input("Name")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button(auth_mode)
    if submitted:
        if auth_mode == "Sign up":
            uid = create_user(email, name or "User", password)
            if uid:
                st.session_state.user = get_user(uid)
                ensure_savings_row(st.session_state.user["id"])
                st.success("Account created & logged in!")
                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
            else:
                st.error("Email already registered.")
        else:
            u = auth_user(email, password)
            if u:
                st.session_state.user = u
                ensure_savings_row(u["id"])
                st.success("Logged in!")
                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
            else:
                st.error("Invalid credentials.")

# If logged in -> main app
if st.session_state.user:
    u = st.session_state.user
    # Ensure savings row exists
    ensure_savings_row(u["id"])

    st.sidebar.markdown(f"**Hello, {u['name']}**")
    if st.sidebar.button("Logout"):
        st.session_state.user = None
        st.session_state.active_gid = None
        st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

    # Sidebar: Groups
    st.sidebar.subheader("Your Groups")
    gs = list_groups(u["id"])
    if gs:
        for g in gs:
            st.sidebar.write(f"- {g['name']}  (code: {g['invite_code']})")
        # selectbox of groups
        g_map = {g["name"] + f"  (code: {g['invite_code']})": g["id"] for g in gs}
        choice = st.sidebar.selectbox("Open a group", list(g_map.keys()))
        st.session_state.active_gid = g_map[choice]
    else:
        st.sidebar.info("Create or join a group to get started.")
        st.session_state.active_gid = None

    with st.sidebar.expander("➕ Create Group"):
        gname = st.text_input("Group name", key="create_group_name")
        short_code = st.checkbox("Use short invite code (6 digits)", value=True, key="short_code_box")
        if st.button("Create", key="create_group_btn"):
            if gname.strip():
                gid = create_group(u["id"], gname, short_code)
                st.success("Group created!")
                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
            else:
                st.error("Enter a group name.")

    with st.sidebar.expander("🔗 Join via Invite Code"):
        code = st.text_input("Invite code", key="invite_code")
        if st.button("Join Group", key="join_btn"):
            if not code.strip():
                st.error("Enter an invite code.")
            else:
                gid, err = join_group(u["id"], code)
                if err:
                    st.error(err)
                else:
                    st.success("Joined!")
                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

    # Sidebar: Progress & badges
    st.sidebar.subheader("🏆 Your Progress")
    total_saved = get_savings(u["id"])
    st.sidebar.metric("Money Saved (₹)", f"{total_saved:,.0f}")
    current_badge = badge_for(total_saved)
    st.sidebar.caption(f"Badge: **{current_badge or '—'}**")

    # Danger: Delete account
    with st.sidebar.expander("⚠️ Danger Zone: My Account"):
        st.write("Delete your account and all data (irreversible).")
        if st.button("Delete My Account & Data"):
            clear_user_data(u["id"])
            st.success("Your account and all data were deleted.")
            st.session_state.user = None
            st.session_state.active_gid = None
            st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

    # Main content: group selected?
    if not st.session_state.active_gid:
        st.title(APP_NAME)
        st.write("Welcome! Create or join a group from the sidebar to begin.")
    else:
        active_gid = st.session_state.active_gid
        g = get_group(active_gid)
        st.header(f"Group: {g['name']}")
        st.caption(f"Invite code: **{g['invite_code']}** – share with trusted people only.")

        # Danger zone: Delete group (owner only)
        if g["owner_id"] == u["id"]:
            with st.expander("⚠️ Danger Zone: Delete Group"):
                st.write("This will delete the group and all posts, votes, notifications.")
                if st.button("Delete this group"):
                    if delete_group(active_gid, u["id"]):
                        st.success("Group deleted.")
                        st.session_state.active_gid = None
                        st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                    else:
                        st.error("Failed to delete group.")

        # Post creation form
        with st.expander("📝 Post an item for advice"):
            with st.form("post_form"):
                item_name = st.text_input("Item name", placeholder="Nike Air Max 270")
                item_link = st.text_input("Link (optional)")
                price = st.number_input("Price (₹)", min_value=0.0, step=100.0)
                reason = st.text_area("Why do you want it?")
                image = st.file_uploader("Image (optional)", type=["jpg", "jpeg", "png"])
                deadline_hours = st.slider("Voting window (hours)", 1, 168, 24)

                # live suggestion
                suggestion = None
                if item_name and item_name.strip():
                    try:
                        suggestion = check_purchase_history(u["id"], item_name.strip(), threshold=0.62)
                    except Exception:
                        suggestion = "✅ Suggestion: Unable to check history right now."

                    if suggestion.startswith("⚠️"):
                        st.warning(suggestion)
                        # unique checkbox key using group id to avoid interference
                        st.session_state.post_auto_skip = st.checkbox(
                            "I already have this / Mark as skipped and record saving",
                            value=False,
                            key=f"post_auto_skip_checkbox_{active_gid}"
                        )
                        if st.session_state.post_auto_skip:
                            st.caption("Submitting will mark the post as SKIPPED and add the price to your saved total.")
                    else:
                        st.success(suggestion)
                        st.session_state.post_auto_skip = False

                submit_post = st.form_submit_button("Post to group")

            if submit_post:
                if not item_name or price <= 0:
                    st.error("Please provide item name and a valid price.")
                else:
                    deadline_dt = datetime.utcnow().replace(tzinfo=tz.tzutc()) + timedelta(hours=deadline_hours)
                    img_bytes = image.read() if image else None
                    pid = create_post(active_gid, u["id"], item_name, item_link, price, reason, img_bytes, deadline_dt)
                    p = get_post(pid)
                    try:
                        if st.session_state.post_auto_skip:
                            decide_post(pid, "skipped")
                            p = get_post(pid)
                            notify_group_new_post(active_gid, p, u["name"])
                            st.success("Posted and recorded as SKIPPED — savings & badge updated.")
                            st.session_state.post_auto_skip = False
                            st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                        else:
                            notify_group_new_post(active_gid, p, u["name"])
                            st.success("Posted! Group notified (if email configured).")
                            st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                    except Exception as e:
                        notify_group_new_post(active_gid, p, u["name"])
                        st.error(f"Posted but failed to finalize skip automatically: {e}")
                        st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()

        st.markdown("---")
        st.subheader("Recent posts in this group")

        posts = list_posts(active_gid)
        if not posts:
            st.info("No posts yet. Create one above.")
        else:
            for p in posts:
                # auto-close if due
                if close_if_due(p):
                    p = get_post(p["id"])

                with st.container():
                    cols = st.columns([1.2, 3, 1.2])
                    with cols[0]:
                        if p.get("image_path") and os.path.exists(p["image_path"]):
                            st.image(p["image_path"], use_column_width=True)
                        st.caption(f"Posted: {to_local(p['created_at'])} IST")
                        poster = get_user(p['user_id'])
                        poster_name = poster['name'] if poster else "Unknown"
                        st.caption(f"By: {poster_name}")

                    with cols[1]:
                        st.subheader(p["item_name"])
                        st.write(f"**Price:** ₹{p['price']:,.0f}")
                        if p.get("item_link"):
                            st.write(f"Link: {p['item_link']}")
                        if p.get("reason"):
                            st.write(f"Reason: {p['reason']}")
                        st.write(f"**Deadline:** {to_local(p['deadline_utc'])} IST")

                        counts, votes_list = vote_counts(p["id"])
                        st.write(
                            f"**Votes →** ✅ Buy: {counts['buy']} | ❌ Don't Buy: {counts['dont_buy']} | 😐 Neutral: {counts['neutral']}"
                        )

                        # show poster suggestion to group
                        try:
                            poster_suggestion = check_purchase_history(p['user_id'], p['item_name'])
                            if poster_suggestion and poster_suggestion.startswith("⚠️"):
                                st.warning(f"(Poster history) {poster_suggestion}")
                        except Exception:
                            pass

                        if p["status"] == "pending":
                            if u["id"] != p["user_id"]:
                                with st.form(f"vote_form_{p['id']}"):
                                    vote = st.radio("Your vote", ["buy", "dont_buy", "neutral"], horizontal=True)
                                    comment = st.text_input("Comment (optional)")
                                    vbtn = st.form_submit_button("Submit vote")
                                if vbtn:
                                    cast_vote(p["id"], u["id"], vote, comment)
                                    st.success("Vote recorded!")
                                    st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                            else:
                                # Poster can finalize anytime
                                st.info("You can finalize decision anytime.")
                        elif p["status"] == "closed":
                            st.warning("Voting closed. Awaiting final decision from poster.")
                        elif p["status"] == "decided":
                            if p["decision"] == "skipped":
                                st.success("Final decision: Skipped ✅ (Saved money)")
                            else:
                                st.info("Final decision: Bought 🛍️")

                        with st.expander("🗨️ See all feedback"):
                            if votes_list:
                                for v in votes_list:
                                    st.write(f"**{v['name']}** → {v['vote']}")
                                    if v.get("comment"):
                                        st.caption(v["comment"])
                            else:
                                st.caption("No votes yet.")

                    with cols[2]:
                        # Poster-only final decision or delete
                        if p["user_id"] == u["id"]:
                            st.write("**Make final decision**")
                            dcol1, dcol2 = st.columns(2)
                            if dcol1.button("I bought it", key=f"buy_{p['id']}"):
                                decide_post(p["id"], "bought")
                                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                            if dcol2.button("I skipped it", key=f"skip_{p['id']}"):
                                decide_post(p["id"], "skipped")
                                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                            if st.button("🗑️ Delete Post", key=f"del_{p['id']}"):
                                ok = delete_post(p["id"], u["id"])
                                if ok:
                                    st.success("Post deleted.")
                                else:
                                    st.error("Cannot delete this post (must be pending and your own).")
                                st.experimental_rerun() if hasattr(st, "experimental_rerun") else st.rerun()
                        else:
                            # For non-poster users, optionally show a 'flag' or contact
                            pass

        st.markdown("---")
        st.subheader("📊 Group Insights")
        decided_posts = [pp for pp in posts if pp["status"] == "decided"][:10]
        if decided_posts:
            bought = sum(1 for pp in decided_posts if pp["decision"] == "bought")
            skipped = sum(1 for pp in decided_posts if pp["decision"] == "skipped")
            st.write(f"Last {len(decided_posts)} decisions → **Bought:** {bought} | **Skipped:** {skipped}")
        else:
            st.caption("No decided posts yet.")

    # footer
    st.markdown("---")
    st.caption("SpendSense: a social accountability app to reduce impulsive buys. Not an expense tracker. Built with Streamlit + SQLite.")
