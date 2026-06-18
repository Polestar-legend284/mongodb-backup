import os
import json
import threading
import datetime
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import Flask, render_template, request, jsonify
from cryptography.fernet import Fernet
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from urllib.parse import quote_plus
import boto3
from botocore.exceptions import NoCredentialsError, ClientError

app = Flask(__name__)

CONFIG_FILE = "config.enc"
KEY_FILE    = "secret.key"
LOG_FILE    = "logs/backup.log"

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("mongo_backup")

backup_status = {
    "last_run":     None,
    "last_result":  None,
    "last_docs":    0,
    "last_size_mb": 0,
    "running":      False,
    "history":      []   # last 10 runs
}

# ── Encryption helpers ─────────────────────────────────────────
def get_or_create_key():
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key

def save_config(data: dict):
    key = get_or_create_key()
    f = Fernet(key)
    encrypted = f.encrypt(json.dumps(data).encode())
    with open(CONFIG_FILE, "wb") as fp:
        fp.write(encrypted)

def load_config() -> dict | None:
    if not os.path.exists(CONFIG_FILE) or not os.path.exists(KEY_FILE):
        return None
    key = get_or_create_key()
    f = Fernet(key)
    with open(CONFIG_FILE, "rb") as fp:
        raw = fp.read()
    try:
        return json.loads(f.decrypt(raw).decode())
    except Exception:
        return None

def config_exists() -> bool:
    return load_config() is not None

# ── Connection testers ─────────────────────────────────────────
def test_mongo(username, password, uri):
    try:
        u = quote_plus(username)
        p = quote_plus(password)
        conn = f"mongodb+srv://{u}:{p}@{uri}/?appName=Cluster0"
        client = MongoClient(conn, server_api=ServerApi("1"), serverSelectionTimeoutMS=8000)
        client.admin.command("ping")
        dbs = [d for d in client.list_database_names() if d not in ("admin","local","config")]
        client.close()
        return True, f"Connected! Found databases: {', '.join(dbs)}"
    except Exception as e:
        return False, str(e)

def test_s3(bucket, region, key_id, secret):
    try:
        s3 = boto3.client("s3", region_name=region,
                          aws_access_key_id=key_id,
                          aws_secret_access_key=secret)
        s3.head_bucket(Bucket=bucket)
        return True, f"S3 bucket '{bucket}' is accessible."
    except NoCredentialsError:
        return False, "Invalid AWS credentials."
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "404":
            return False, f"Bucket '{bucket}' not found."
        return False, str(e)
    except Exception as e:
        return False, str(e)

# ── Email alerts ──────────────────────────────────────────────
def send_failure_email(cfg, error_msg, timestamp):
    if not cfg.get("gmail_addr") or not cfg.get("gmail_pass") or not cfg.get("alert_email"):
        log.warning("Email alerts not configured, skipping.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"⚠️ MongoDB Backup FAILED — {timestamp}"
        msg["From"]    = cfg["gmail_addr"]
        msg["To"]      = cfg["alert_email"]

        body = f"""
        <html><body style="font-family:sans-serif;color:#1e293b;padding:24px;">
          <h2 style="color:#ef4444;">⚠️ Backup Failed</h2>
          <p>Your MongoDB backup failed at <strong>{timestamp}</strong>.</p>
          <div style="background:#fef2f2;border:1px solid #fecaca;border-radius:8px;padding:16px;margin:16px 0;">
            <strong>Error:</strong><br>
            <code style="color:#b91c1c;">{error_msg}</code>
          </div>
          <p>Please check <code>logs/backup.log</code> for more details and make sure your MongoDB and AWS credentials are still valid.</p>
          <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">
          <p style="color:#94a3b8;font-size:12px;">MongoDB Backup System — Cloud Counselage Internship Project</p>
        </body></html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(cfg["gmail_addr"], cfg["gmail_pass"])
            server.sendmail(cfg["gmail_addr"], cfg["alert_email"], msg.as_string())

        log.info(f"Failure alert sent to {cfg['alert_email']}")
    except Exception as e:
        log.error(f"Could not send failure email: {e}")

def send_success_email(cfg, total_docs, size_mb, elapsed, timestamp):
    if not cfg.get("gmail_addr") or not cfg.get("gmail_pass") or not cfg.get("alert_email"):
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"✅ MongoDB Backup Successful — {timestamp}"
        msg["From"]    = cfg["gmail_addr"]
        msg["To"]      = cfg["alert_email"]

        body = f"""
        <html><body style="font-family:sans-serif;color:#1e293b;padding:24px;">
          <h2 style="color:#22c55e;">✅ Backup Successful</h2>
          <p>Your MongoDB backup completed successfully at <strong>{timestamp}</strong>.</p>
          <table style="border-collapse:collapse;margin:16px 0;">
            <tr><td style="padding:8px 16px 8px 0;color:#64748b;">Documents backed up</td><td><strong>{total_docs:,}</strong></td></tr>
            <tr><td style="padding:8px 16px 8px 0;color:#64748b;">Backup size</td><td><strong>{size_mb} MB</strong></td></tr>
            <tr><td style="padding:8px 16px 8px 0;color:#64748b;">Duration</td><td><strong>{elapsed}s</strong></td></tr>
            <tr><td style="padding:8px 16px 8px 0;color:#64748b;">Uploaded to</td><td><strong>AWS S3 — {cfg.get('aws_bucket','')}</strong></td></tr>
          </table>
          <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">
          <p style="color:#94a3b8;font-size:12px;">MongoDB Backup System — Cloud Counselage Internship Project</p>
        </body></html>
        """
        msg.attach(MIMEText(body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(cfg["gmail_addr"], cfg["gmail_pass"])
            server.sendmail(cfg["gmail_addr"], cfg["alert_email"], msg.as_string())

        log.info(f"Success notification sent to {cfg['alert_email']}")
    except Exception as e:
        log.error(f"Could not send success email: {e}")

# ── Backup engine ──────────────────────────────────────────────
def run_backup_job(cfg):
    import zipfile, shutil, time
    global backup_status

    backup_status["running"] = True
    start = datetime.datetime.now()
    log.info("Backup started")

    try:
        u = quote_plus(cfg["mongo_username"])
        p = quote_plus(cfg["mongo_password"])
        conn = f"mongodb+srv://{u}:{p}@{cfg['mongo_uri']}/?appName=Cluster0"
        client = MongoClient(conn, server_api=ServerApi("1"), serverSelectionTimeoutMS=10000)
        client.admin.command("ping")

        db_names = client.list_database_names()
        timestamp = start.strftime("%Y%m%d_%H%M%S")
        folder = f"backup_{timestamp}"
        os.makedirs(folder, exist_ok=True)
        total_docs = 0

        for db_name in db_names:
            if db_name in ("admin", "local", "config"):
                continue
            db = client[db_name]
            db_folder = os.path.join(folder, db_name)
            os.makedirs(db_folder, exist_ok=True)
            for col_name in db.list_collection_names():
                try:
                    import json as _json
                    docs = list(db[col_name].find({}, {"_id": 0}))
                    with open(os.path.join(db_folder, f"{col_name}.json"), "w") as fp:
                        _json.dump(docs, fp, indent=2, default=str)
                    total_docs += len(docs)
                    log.info(f"  {db_name}.{col_name}: {len(docs)} docs")
                except Exception as e:
                    log.warning(f"  {db_name}.{col_name} failed: {e}")

        client.close()

        zip_name = f"{folder}.zip"
        with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(folder):
                for file in files:
                    zf.write(os.path.join(root, file))
        shutil.rmtree(folder)
        size_mb = round(os.path.getsize(zip_name) / (1024*1024), 2)

        # Upload to S3
        s3 = boto3.client("s3", region_name=cfg["aws_region"],
                          aws_access_key_id=cfg["aws_key_id"],
                          aws_secret_access_key=cfg["aws_secret"])
        s3_key = f"backups/{zip_name}"
        s3.upload_file(zip_name, cfg["aws_bucket"], s3_key)
        log.info(f"Uploaded to S3: {s3_key}")

        # Cleanup old local backups (keep 7)
        backups = sorted([f for f in os.listdir(".") if f.startswith("backup_") and f.endswith(".zip")])
        for old in backups[:-7]:
            os.remove(old)

        elapsed = (datetime.datetime.now() - start).seconds
        result = {
            "time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "success",
            "docs": total_docs,
            "size_mb": size_mb,
            "elapsed_s": elapsed,
            "s3_path": f"s3://{cfg['aws_bucket']}/{s3_key}"
        }
        backup_status.update({
            "last_run": result["time"],
            "last_result": "success",
            "last_docs": total_docs,
            "last_size_mb": size_mb,
        })
        backup_status["history"] = ([result] + backup_status["history"])[:10]
        log.info(f"Backup complete: {total_docs} docs, {size_mb} MB, {elapsed}s")
        send_success_email(cfg, total_docs, size_mb, elapsed, result["time"])

    except Exception as e:
        result = {
            "time": start.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "failed",
            "error": str(e)
        }
        backup_status.update({"last_run": result["time"], "last_result": "failed"})
        backup_status["history"] = ([result] + backup_status["history"])[:10]
        log.error(f"Backup failed: {e}")
        send_failure_email(cfg, str(e), result["time"])
    finally:
        backup_status["running"] = False

# ── Restore engine ────────────────────────────────────────────
restore_status = {
    "running": False,
    "progress": "",
    "result": None
}

def list_s3_backups(cfg):
    s3 = boto3.client("s3", region_name=cfg["aws_region"],
                      aws_access_key_id=cfg["aws_key_id"],
                      aws_secret_access_key=cfg["aws_secret"])
    response = s3.list_objects_v2(Bucket=cfg["aws_bucket"], Prefix="backups/")
    files = []
    for obj in response.get("Contents", []):
        key = obj["Key"]
        if key.endswith(".zip"):
            files.append({
                "key": key,
                "name": key.replace("backups/", ""),
                "size_mb": round(obj["Size"] / (1024*1024), 2),
                "last_modified": obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S")
            })
    return sorted(files, key=lambda x: x["name"], reverse=True)

def run_restore_job(cfg, s3_key):
    import zipfile, shutil
    global restore_status
    restore_status["running"] = True
    restore_status["result"] = None

    try:
        # Download from S3
        restore_status["progress"] = "Downloading backup from S3..."
        log.info(f"Restore started: {s3_key}")
        s3 = boto3.client("s3", region_name=cfg["aws_region"],
                          aws_access_key_id=cfg["aws_key_id"],
                          aws_secret_access_key=cfg["aws_secret"])
        local_zip = f"restore_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        s3.download_file(cfg["aws_bucket"], s3_key, local_zip)

        # Extract zip
        restore_status["progress"] = "Extracting backup..."
        extract_folder = local_zip.replace(".zip", "")
        with zipfile.ZipFile(local_zip, "r") as zf:
            zf.extractall(extract_folder)
        os.remove(local_zip)

        # Connect to MongoDB
        restore_status["progress"] = "Connecting to MongoDB..."
        u = quote_plus(cfg["mongo_username"])
        p = quote_plus(cfg["mongo_password"])
        conn = f"mongodb+srv://{u}:{p}@{cfg['mongo_uri']}/?appName=Cluster0"
        client = MongoClient(conn, server_api=ServerApi("1"), serverSelectionTimeoutMS=10000)
        client.admin.command("ping")

        # Walk extracted folders and restore
        total_docs = 0
        import json as _json
        for db_name in os.listdir(extract_folder):
            db_path = os.path.join(extract_folder, db_name)
            if not os.path.isdir(db_path):
                continue
            db = client[db_name]
            for json_file in os.listdir(db_path):
                if not json_file.endswith(".json"):
                    continue
                col_name = json_file.replace(".json", "")
                restore_status["progress"] = f"Restoring {db_name}.{col_name}..."
                with open(os.path.join(db_path, json_file), "r") as f:
                    docs = _json.load(f)
                if docs:
                    col = db[col_name]
                    col.delete_many({})       # clear existing
                    col.insert_many(docs)
                    total_docs += len(docs)
                    log.info(f"  Restored {db_name}.{col_name}: {len(docs)} docs")

        client.close()
        shutil.rmtree(extract_folder)

        restore_status["result"] = {"status": "success", "docs": total_docs, "source": s3_key}
        restore_status["progress"] = f"Done! Restored {total_docs:,} documents."
        log.info(f"Restore complete: {total_docs} docs from {s3_key}")

    except Exception as e:
        restore_status["result"] = {"status": "failed", "error": str(e)}
        restore_status["progress"] = f"Failed: {e}"
        log.error(f"Restore failed: {e}")
    finally:
        restore_status["running"] = False

# ── Routes ─────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html", configured=config_exists())

@app.route("/api/status")
def api_status():
    cfg = load_config()
    return jsonify({
        "configured": cfg is not None,
        "backup": backup_status,
        "schedule": "Daily at 02:00 AM"
    })

@app.route("/api/test-mongo", methods=["POST"])
def api_test_mongo():
    d = request.json
    ok, msg = test_mongo(d["username"], d["password"], d["uri"])
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/test-s3", methods=["POST"])
def api_test_s3():
    d = request.json
    ok, msg = test_s3(d["bucket"], d["region"], d["key_id"], d["secret"])
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/save-config", methods=["POST"])
def api_save_config():
    d = request.json
    save_config(d)
    log.info("Configuration saved")
    return jsonify({"ok": True})

@app.route("/api/run-now", methods=["POST"])
def api_run_now():
    if backup_status["running"]:
        return jsonify({"ok": False, "message": "Backup already running."})
    cfg = load_config()
    if not cfg:
        return jsonify({"ok": False, "message": "Not configured yet."})
    threading.Thread(target=run_backup_job, args=(cfg,), daemon=True).start()
    return jsonify({"ok": True, "message": "Backup started!"})

@app.route("/api/reset-config", methods=["POST"])
def api_reset_config():
    for f in [CONFIG_FILE, KEY_FILE]:
        if os.path.exists(f):
            os.remove(f)
    return jsonify({"ok": True})

@app.route("/api/list-backups")
def api_list_backups():
    cfg = load_config()
    if not cfg:
        return jsonify({"ok": False, "backups": []})
    try:
        backups = list_s3_backups(cfg)
        return jsonify({"ok": True, "backups": backups})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "backups": []})

@app.route("/api/restore", methods=["POST"])
def api_restore():
    if restore_status["running"]:
        return jsonify({"ok": False, "message": "Restore already in progress."})
    if backup_status["running"]:
        return jsonify({"ok": False, "message": "Cannot restore while backup is running."})
    cfg = load_config()
    if not cfg:
        return jsonify({"ok": False, "message": "Not configured."})
    s3_key = request.json.get("key")
    if not s3_key:
        return jsonify({"ok": False, "message": "No backup key provided."})
    threading.Thread(target=run_restore_job, args=(cfg, s3_key), daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/restore-status")
def api_restore_status():
    return jsonify(restore_status)

if __name__ == "__main__":
    import webbrowser, sys, os

    # When packaged as exe, templates/static are next to the binary
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
        app.template_folder = os.path.join(base, "templates")

    print("\n  MongoDB Backup System")
    print("  Opening http://localhost:5000 in your browser...\n")

    # Open browser after a short delay so Flask is ready
    def open_browser():
        try:
            webbrowser.open("http://localhost:5000")
        except Exception:
            pass  # if it fails, user sees the URL in terminal anyway
    threading.Timer(2.0, open_browser).start()
    app.run(debug=False, port=5000)
