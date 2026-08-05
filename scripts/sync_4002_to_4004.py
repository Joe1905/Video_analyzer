import sqlite3
import shutil
import os
from pathlib import Path

SRC_DB = "/home/openclaw/Video_analyzer/data/hot_video_report.sqlite"
DST_DB = "/home/openclaw/Video_analyzer-ui-4004/data/hot_video_report.sqlite"

SRC_VIDEOS = "/home/openclaw/Video_analyzer/videos"
DST_VIDEOS = "/home/openclaw/Video_analyzer-ui-4004/videos"

SRC_OUTPUT = "/home/openclaw/Video_analyzer/output"
DST_OUTPUT = "/home/openclaw/Video_analyzer-ui-4004/output"

SRC_COVERS = "/home/openclaw/Video_analyzer/data/report_covers"
DST_COVERS = "/home/openclaw/Video_analyzer-ui-4004/data/report_covers"

def sync_data(date_str="2026-08-04"):
    print(f"=== Syncing data for date {date_str} from 4002 to 4004 ===")
    
    if not os.path.exists(SRC_DB):
        print(f"Source DB {SRC_DB} does not exist.")
        return

    src_conn = sqlite3.connect(SRC_DB)
    src_conn.row_factory = sqlite3.Row
    dst_conn = sqlite3.connect(DST_DB)
    dst_conn.row_factory = sqlite3.Row

    # 1. Sync tables schema & records
    src_cur = src_conn.cursor()
    dst_cur = dst_conn.cursor()

    tables = [t[0] for t in src_cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    print(f"Tables in source DB: {tables}")

    for table in tables:
        # Check columns
        src_cols = [c[1] for c in src_cur.execute(f"PRAGMA table_info('{table}')").fetchall()]
        print(f"Table '{table}' columns: {src_cols}")

        if "report_date" in src_cols or "date" in src_cols:
            col_name = "report_date" if "report_date" in src_cols else "date"
            rows = src_cur.execute(f"SELECT * FROM '{table}' WHERE {col_name} = ?", (date_str,)).fetchall()
            print(f"Found {len(rows)} rows in '{table}' for date {date_str}")
            
            if rows:
                placeholders = ",".join(["?"] * len(src_cols))
                cols_str = ",".join([f"'{c}'" for c in src_cols])
                
                # Delete existing row in DST for that date
                dst_cur.execute(f"DELETE FROM '{table}' WHERE {col_name} = ?", (date_str,))
                
                for r in rows:
                    dst_cur.execute(f"INSERT OR REPLACE INTO '{table}' ({cols_str}) VALUES ({placeholders})", tuple(r))
                print(f"Synced {len(rows)} rows into DST '{table}'")

    dst_conn.commit()
    print("Database sync completed.")

    # 2. Sync video files and output folders
    if os.path.exists(SRC_VIDEOS) and os.path.exists(DST_VIDEOS):
        for f in os.listdir(SRC_VIDEOS):
            if date_str in f or f.endswith(".mp4") or f.endswith(".jpg") or f.endswith(".png"):
                src_f = os.path.join(SRC_VIDEOS, f)
                dst_f = os.path.join(DST_VIDEOS, f)
                if os.path.isfile(src_f) and not os.path.exists(dst_f):
                    shutil.copy2(src_f, dst_f)
                    print(f"Copied video: {f}")

    if os.path.exists(SRC_COVERS) and os.path.exists(DST_COVERS):
        for f in os.listdir(SRC_COVERS):
            src_f = os.path.join(SRC_COVERS, f)
            dst_f = os.path.join(DST_COVERS, f)
            if os.path.isfile(src_f) and not os.path.exists(dst_f):
                shutil.copy2(src_f, dst_f)

    print("=== Sync Finished Successfully ===")

if __name__ == "__main__":
    sync_data("2026-08-04")
