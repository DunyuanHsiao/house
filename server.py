"""法拍屋追蹤 — 本機網頁伺服器。

用法:
  python3 server.py                  # http://localhost:8000 ，每 6 小時自動更新
  python3 server.py --port 8080 --interval 2   # 每 2 小時更新
  python3 server.py --interval 0     # 不自動更新
"""

import argparse
import json
import sqlite3
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import scraper

STATIC = Path(__file__).parent / "static"
_scrape_lock = threading.Lock()


def db():
    conn = scraper.init_db()
    return conn


def rows(sql, args=()):
    conn = db()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def do_scrape():
    if not _scrape_lock.acquire(blocking=False):
        return {"error": "已在更新中"}
    try:
        return scraper.run(log=lambda m: print(f"[{datetime.now():%H:%M:%S}] {m}"))
    except Exception as e:
        print(f"[scrape] 失敗: {e}")
        return {"error": str(e)}
    finally:
        _scrape_lock.release()


def scheduler(hours):
    while True:
        do_scrape()
        time.sleep(hours * 3600)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(STATIC), **kw)

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/api/lots":
            # 只顯示 config.json 設定的縣市；不在設定裡的縣市已停止更新，資料會過時
            counties = scraper.load_counties()
            where = f"WHERE county IN ({','.join('?' * len(counties))})" if counties else ""
            data = rows(f"SELECT * FROM lots {where} ORDER BY saledate DESC", counties)
            for d in data:
                d["addresses"] = json.loads(d["addresses"] or "[]")
            return self.send_json({
                "lots": data,
                "pdf_url": scraper.PDF_URL,
                "pic_url": scraper.PIC_URL,
            })
        if url.path == "/api/history":
            key = q.get("key", [""])[0]
            return self.send_json(rows(
                "SELECT * FROM rounds WHERE lot_key=? ORDER BY saledate, saletype", (key,)))
        if url.path == "/api/status":
            last = rows("SELECT * FROM runs ORDER BY id DESC LIMIT 1")
            ok = rows("SELECT * FROM runs WHERE error IS NULL AND finished IS NOT NULL ORDER BY id DESC LIMIT 1")
            return self.send_json({
                "last_run": last[0] if last else None,
                "last_success": ok[0] if ok else None,
                "running": _scrape_lock.locked(),
                "counties": scraper.load_counties(),
            })
        return super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path == "/api/refresh":
            return self.send_json(do_scrape())
        self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--interval", type=float, default=6, help="自動更新間隔（小時），0 = 關閉")
    args = ap.parse_args()

    scraper.init_db().close()
    if args.interval > 0:
        threading.Thread(target=scheduler, args=(args.interval,), daemon=True).start()
    print(f"法拍屋追蹤: http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
