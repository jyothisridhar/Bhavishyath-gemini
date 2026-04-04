"""
Bhavishyat Admin Tool — View conversation logs from the terminal.
Usage: python admin.py [command]

Commands:
  logs          - Show recent conversations (default: last 50 messages)
  users         - List all users with profiles
  user <id>     - Show full conversation for a specific user
  stats         - Show basic usage stats
"""

import sqlite3
import json
import sys
import os
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "bhavishyat.db")


def get_conn():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}. Has the bot run yet?")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)


def show_logs(limit=50):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, user_id, username, role, message
        FROM conversation_log
        ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = c.fetchall()
    conn.close()

    print(f"\n{'='*70}")
    print(f"  RECENT CONVERSATIONS (last {limit} messages)")
    print(f"{'='*70}\n")

    for ts, uid, uname, role, msg in reversed(rows):
        time_str = ts[:16].replace("T", " ")
        label = "🧑 STUDENT" if role == "user" else "🤖 BOT    "
        if role == "system":
            label = "⚠️  SYSTEM "
        uname_str = f"@{uname}" if uname else f"id:{uid}"
        print(f"[{time_str}] {label} ({uname_str})")
        print(f"  {msg[:300]}{'...' if len(msg) > 300 else ''}")
        print()


def show_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT u.user_id, u.username, u.first_name, u.profile_json, u.updated_at,
               COUNT(l.id) as msg_count
        FROM user_memory u
        LEFT JOIN conversation_log l ON l.user_id = u.user_id
        GROUP BY u.user_id
        ORDER BY u.updated_at DESC
    """)
    rows = c.fetchall()
    conn.close()

    print(f"\n{'='*70}")
    print(f"  ALL USERS ({len(rows)} total)")
    print(f"{'='*70}\n")

    for uid, uname, fname, profile_json, updated, msg_count in rows:
        profile = json.loads(profile_json) if profile_json else {}
        print(f"ID: {uid} | @{uname or 'N/A'} | {fname}")
        print(f"  Messages: {msg_count} | Last active: {(updated or '')[:16].replace('T',' ')}")
        if profile:
            print(f"  Profile: {json.dumps(profile, ensure_ascii=False)}")
        print()


def show_user(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT timestamp, role, message FROM conversation_log
        WHERE user_id = ? ORDER BY id
    """, (user_id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        print(f"No conversations found for user {user_id}")
        return

    print(f"\n{'='*70}")
    print(f"  CONVERSATION HISTORY — User {user_id} ({len(rows)} messages)")
    print(f"{'='*70}\n")

    for ts, role, msg in rows:
        time_str = ts[:16].replace("T", " ")
        if role == "user":
            print(f"[{time_str}] 🧑 STUDENT:")
        elif role == "assistant":
            print(f"[{time_str}] 🤖 BOT:")
        else:
            print(f"[{time_str}] ⚠️  SYSTEM:")
        print(f"  {msg}\n")


def show_stats():
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT COUNT(DISTINCT user_id) FROM conversation_log")
    total_users = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM conversation_log WHERE role = 'user'")
    total_messages = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*) FROM conversation_log
        WHERE timestamp >= date('now', '-7 days')
        AND role = 'user'
    """)
    messages_7d = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*) FROM conversation_log
        WHERE message LIKE '%CRISIS%'
    """)
    crisis_flags = c.fetchone()[0]

    conn.close()

    print(f"\n{'='*70}")
    print(f"  BHAVISHYAT BOT STATS")
    print(f"{'='*70}\n")
    print(f"  Total unique users:       {total_users}")
    print(f"  Total student messages:   {total_messages}")
    print(f"  Messages (last 7 days):   {messages_7d}")
    print(f"  Crisis flags:             {crisis_flags}")
    print()


def main():
    args = sys.argv[1:]
    if not args or args[0] == "logs":
        limit = int(args[1]) if len(args) > 1 else 50
        show_logs(limit)
    elif args[0] == "users":
        show_users()
    elif args[0] == "user" and len(args) > 1:
        show_user(int(args[1]))
    elif args[0] == "stats":
        show_stats()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
