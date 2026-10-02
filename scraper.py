"""台北市法拍屋爬蟲：從司法院「法拍屋查詢系統」抓取資料並寫入 SQLite。

資料來源: https://aomp109.judicial.gov.tw/judbp/wkw/WHD1A02.htm
抓取三種資料（皆限臺北市、房屋類）:
  saletype=1 一般程序（進行中的拍賣）
  saletype=4 應買公告（特別程序）
  saletype=5 拍定價格（已拍定結果）

用法:
  python3 scraper.py            # 抓一次
  python3 scraper.py --county A # 縣市代碼，A=臺北市
"""

import argparse
import hashlib
import http.cookiejar
import json
import re
import sqlite3
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

BASE = "https://aomp109.judicial.gov.tw/judbp/wkw"
PDF_URL = BASE + "/WHD1A02/DO_VIEWPDF.htm?filenm="
PIC_URL = "https://kpic.judicial.gov.tw/judkp/wkw/WHD1A02_DETAIL.htm?para="
DB_PATH = Path(__file__).parent / "data" / "houses.db"
PAGE_SIZE = 200
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"

SALETYPES = {"1": "一般程序", "4": "應買公告", "5": "拍定"}
CHECKYN = {"Y": "點交", "N": "不點交", "M": "如備註"}
SQM_TO_PING = 0.3025

SCHEMA = """
CREATE TABLE IF NOT EXISTS lots (
    lot_key      TEXT PRIMARY KEY,
    crtid        TEXT, crtnm TEXT, crm TEXT, dpt TEXT,
    district     TEXT, sec TEXT,
    addresses    TEXT,              -- JSON array
    area_m2      REAL,
    rrange       TEXT,              -- 權利範圍：全部 / 持分
    checkyn      TEXT, emptyyn TEXT, comm_yn TEXT,
    status       TEXT,              -- active / special / stopped / sold / ended
    saletype     TEXT,
    saleno       TEXT, salenostr TEXT,
    saledate     TEXT,              -- ISO 日期（最近一次拍賣）
    min_price    INTEGER,           -- 最近一次總拍賣底價
    first_price  INTEGER,           -- 首次看到的總底價
    sold_price   INTEGER, sold_date TEXT,
    rmk          TEXT,
    filenm       TEXT, para TEXT, pic_cnt INTEGER,
    first_seen   TEXT, last_seen TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS rounds (
    lot_key   TEXT, saletype TEXT, saledate TEXT,
    saleno    TEXT, salenostr TEXT,
    min_price INTEGER, sold_price INTEGER,
    rmk       TEXT, filenm TEXT,
    first_seen TEXT,
    PRIMARY KEY (lot_key, saletype, saledate)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT, finished TEXT,
    n_rows INTEGER, n_lots INTEGER, n_new INTEGER, n_changed INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_lots_status ON lots(status);
"""


class JudicialClient:
    """處理 session cookie、CSRF 與 token；查詢 API 需要 Referer。"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        # 司法院憑證缺少 Subject Key Identifier，Python 3.13+ 的嚴格模式會拒絕；
        # 只關掉 strict 旗標，仍保留一般憑證驗證
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), urllib.request.HTTPSHandler(context=ctx))
        self.csrf = self.token = None

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with self.opener.open(req, timeout=60) as r:
            return r.read().decode("utf-8", "replace")

    def init_session(self):
        self._get(BASE + "/WHD1A02.htm")
        html = self._get(BASE + "/WHD1A02/V2.htm")
        self.csrf = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
        self.token = re.search(r'name="token" value="([^"]+)"', html).group(1)

    def query(self, county, saletype, page, proptype="C52"):
        form = {
            "county": county, "proptype": proptype, "saletype": saletype,
            "sorted_column": "A.CRMYY, A.CRMID, A.CRMNO, A.SALENO, A.ROWID",
            "sorted_type": "ASC", "pageNum": page, "pageSize": PAGE_SIZE,
            "token": self.token, "_csrf": self.csrf,
        }
        req = urllib.request.Request(
            BASE + "/WHD1A02/QUERY.htm",
            data=urllib.parse.urlencode(form).encode(),
            headers={
                "User-Agent": UA,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": BASE + "/WHD1A02/V2.htm",
            },
        )
        with self.opener.open(req, timeout=90) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if resp.get("data") is None:
            raise RuntimeError(f"查詢失敗: {resp.get('messageText')}")
        return resp["data"], resp["pageInfo"]["totalNum"]

    def fetch_all(self, county, saletype):
        rows, page = [], 1
        while True:
            data, total = self.query(county, saletype, page)
            rows.extend(data)
            if not data or len(rows) >= total:
                return rows
            page += 1
            time.sleep(1)  # 對官方網站客氣一點


def roc_to_iso(s):
    """民國日期 '1151012' -> '2026-10-12'"""
    if not s or len(s) < 7:
        return None
    y, m, d = int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:])
    return date(y, m, d).isoformat()


def norm_addr(r):
    a = (r.get("budadd") or "").strip()
    city = r.get("hsimun") or ""
    if a and not a.startswith(city):
        a = city + (r.get("ctmd") or "") + a
    return a


def group_lots(rows, saletype):
    """把同一案號、同一標別、同一拍賣日的多筆建物合併成一個「標的」。"""
    groups = {}
    for r in rows:
        k = (r["crtid"], r["crmyy"], r["crmid"], r["crmno"], r["batchno"], r["saledate"])
        groups.setdefault(k, []).append(r)

    lots = []
    for rs in groups.values():
        r0 = rs[0]
        addrs = sorted({norm_addr(r) for r in rs if norm_addr(r)})
        # 同地址同面積的列是同一棟建物搭配不同土地，不重複加總面積
        area = sum(b for _, b in {(norm_addr(r), r["btotal"] or 0) for r in rs})
        ident = f'{r0["crtid"]}|{r0["crmyy"]}{r0["crmid"]}{r0["crmno"]}|{"/".join(addrs)}'
        lot_key = hashlib.sha1(ident.encode()).hexdigest()[:16]
        rmk = r0.get("rmkexcel") or ""
        partial = any((r.get("rrange") or "") != "全部" for r in rs)
        if saletype == "5":
            status = "sold"
        elif rmk and "更正" not in rmk:
            status = "stopped"
        else:
            status = "special" if saletype == "4" else "active"
        lots.append({
            "lot_key": lot_key,
            "crtid": r0["crtid"], "crtnm": r0["crtnm"], "crm": r0["crm"], "dpt": r0["dpt"],
            "district": r0["ctmd"], "sec": r0["sec"],
            "addresses": json.dumps(addrs, ensure_ascii=False),
            "area_m2": round(area, 2),
            "rrange": "持分" if partial else "全部",
            "checkyn": CHECKYN.get(r0["checkyn"], r0["checkyn"]),
            "emptyyn": "空屋" if r0.get("emptyyn") == "Y" else "",
            "comm_yn": r0.get("comm_yn"),
            "status": status, "saletype": saletype,
            "saleno": r0["saleno"], "salenostr": r0["salenostr"] or SALETYPES[saletype],
            "saledate": roc_to_iso(r0["saledate"]),
            "min_price": r0["summinprc"],
            "sold_price": r0["sumprice"] if saletype == "5" else None,
            "rmk": rmk,
            "filenm": r0["filenm"], "para": r0["para"], "pic_cnt": r0.get("pic_cnt") or 0,
        })
    return lots


def init_db(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn, lot, now):
    cur = conn.execute("SELECT * FROM lots WHERE lot_key=?", (lot["lot_key"],)).fetchone()
    new = cur is None
    changed = False

    if new:
        fields = dict(lot, first_price=lot["min_price"], first_seen=now, last_seen=now, updated_at=now)
        if lot["status"] == "sold":
            fields["sold_date"] = lot["saledate"]
        cols = ",".join(fields)
        conn.execute(f"INSERT INTO lots ({cols}) VALUES ({','.join('?' * len(fields))})",
                     list(fields.values()))
    else:
        cur = dict(cur)
        if lot["status"] == "sold":
            # 拍定結果只更新成交欄位，保留拍賣中的資訊
            if cur["status"] != "sold":
                changed = True
            conn.execute(
                "UPDATE lots SET status='sold', sold_price=?, sold_date=?, last_seen=?, updated_at=? WHERE lot_key=?",
                (lot["sold_price"], lot["saledate"], now, now if changed else cur["updated_at"], lot["lot_key"]))
        elif cur["status"] == "sold" or (cur["saledate"] or "") > (lot["saledate"] or ""):
            # 已拍定，或這筆是較舊的拍次資料：只更新 last_seen
            conn.execute("UPDATE lots SET last_seen=? WHERE lot_key=?", (now, lot["lot_key"]))
        else:
            watch = ("status", "saleno", "saledate", "min_price", "rmk")
            changed = any(cur[k] != lot[k] for k in watch)
            sets = {k: v for k, v in lot.items() if k != "lot_key"}
            sets["last_seen"] = now
            if changed:
                sets["updated_at"] = now
            conn.execute(f"UPDATE lots SET {','.join(k + '=?' for k in sets)} WHERE lot_key=?",
                         [*sets.values(), lot["lot_key"]])

    conn.execute(
        """INSERT INTO rounds (lot_key, saletype, saledate, saleno, salenostr, min_price, sold_price, rmk, filenm, first_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(lot_key, saletype, saledate) DO UPDATE SET
             saleno=excluded.saleno, salenostr=excluded.salenostr, min_price=excluded.min_price,
             sold_price=excluded.sold_price, rmk=excluded.rmk, filenm=excluded.filenm""",
        (lot["lot_key"], lot["saletype"], lot["saledate"], lot["saleno"], lot["salenostr"],
         lot["min_price"], lot["sold_price"], lot["rmk"], lot["filenm"], now))
    return new, changed


def run(county="A", db_path=DB_PATH, log=print):
    conn = init_db(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    run_id = conn.execute("INSERT INTO runs (started) VALUES (?)", (now,)).lastrowid
    conn.commit()
    n_rows = n_lots = n_new = n_changed = 0
    try:
        client = JudicialClient()
        client.init_session()
        seen = set()
        # 先處理拍賣中，再處理拍定，讓拍定狀態覆蓋
        for st in ("1", "4", "5"):
            rows = client.fetch_all(county, st)
            lots = group_lots(rows, st)
            log(f"[{SALETYPES[st]}] {len(rows)} 筆建物 → {len(lots)} 個標的")
            n_rows += len(rows)
            n_lots += len(lots)
            for lot in lots:
                new, changed = upsert(conn, lot, now)
                n_new += new
                n_changed += changed
                if st != "5":
                    seen.add(lot["lot_key"])
            time.sleep(1)

        # 之前在拍賣中、這次沒出現且拍賣日已過 → 結束（未拍定或撤回）
        today = date.today().isoformat()
        for row in conn.execute(
                "SELECT lot_key FROM lots WHERE status IN ('active','special','stopped') AND saledate < ?",
                (today,)).fetchall():
            if row["lot_key"] not in seen:
                conn.execute("UPDATE lots SET status='ended', updated_at=? WHERE lot_key=?",
                             (now, row["lot_key"]))
        conn.execute("UPDATE runs SET finished=?, n_rows=?, n_lots=?, n_new=?, n_changed=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), n_rows, n_lots, n_new, n_changed, run_id))
        conn.commit()
        log(f"完成：新增 {n_new}，異動 {n_changed}")
        return {"new": n_new, "changed": n_changed, "lots": n_lots}
    except Exception as e:
        conn.rollback()
        conn.execute("UPDATE runs SET finished=?, error=? WHERE id=?",
                     (datetime.now().isoformat(timespec="seconds"), repr(e), run_id))
        conn.commit()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--county", default="A", help="縣市代碼 (A=臺北市)")
    args = ap.parse_args()
    try:
        run(args.county)
    except Exception as e:
        print(f"錯誤: {e}", file=sys.stderr)
        sys.exit(1)
