"""
Greenlight monitor — prints live stats every 60 seconds.
Run: python3 monitor.py
"""
import sqlite3, time, os
from datetime import datetime

DB = "greenlight.db"

def stats():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    total_keys = conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0]
    total_reqs = conn.execute("SELECT COUNT(*) FROM approval_requests").fetchone()[0]
    pending    = conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0]
    decided    = conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status='decided'").fetchone()[0]
    recent     = conn.execute("""
        SELECT title, status, decision, created_at
        FROM approval_requests ORDER BY created_at DESC LIMIT 5
    """).fetchall()
    conn.close()
    return total_keys, total_reqs, pending, decided, recent

def main():
    print("📊 Greenlight Monitor — refreshing every 60s. Ctrl+C to stop.\n")
    while True:
        os.system("clear")
        keys, reqs, pending, decided, recent = stats()
        print(f"=== Greenlight Live Stats — {datetime.now().strftime('%H:%M:%S')} ===")
        print(f"  API keys registered : {keys}")
        print(f"  Total requests      : {reqs}")
        print(f"  Pending             : {pending}")
        print(f"  Decided             : {decided}")
        print(f"\n  Recent requests:")
        for r in recent:
            print(f"    [{r['status']:7}] {r['title'][:50]} ({r['created_at'][:16]})")
        print(f"\n  URL: http://34.24.181.67")
        time.sleep(60)

if __name__ == "__main__":
    main()
