import os
import sys
import datetime
import schedule
import time
import zipfile
import boto3
import logging
from pymongo import MongoClient
from pymongo.server_api import ServerApi
from urllib.parse import quote_plus
from botocore.exceptions import NoCredentialsError, ClientError
from dotenv import load_dotenv
import json
import shutil

# ─────────────────────────────────────────────
#  Load .env file
# ─────────────────────────────────────────────
load_dotenv()

# ─────────────────────────────────────────────
#  Terminal colors (work on Windows too via ANSI)
# ─────────────────────────────────────────────
os.system("")  # enables ANSI on Windows terminal

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):    print(f"{GREEN}  [OK]{RESET} {msg}")
def info(msg):  print(f"{CYAN} [INFO]{RESET} {msg}")
def warn(msg):  print(f"{YELLOW} [WARN]{RESET} {msg}")
def fail(msg):  print(f"{RED} [FAIL]{RESET} {msg}")

# ─────────────────────────────────────────────
#  Logging to file
# ─────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("logs/backup.log"),
    ]
)
log = logging.getLogger("mongo_backup")

# ─────────────────────────────────────────────
#  Read config from environment
# ─────────────────────────────────────────────
def load_config():
    required = {
        "MONGO_USERNAME":        os.getenv("MONGO_USERNAME"),
        "MONGO_PASSWORD":        os.getenv("MONGO_PASSWORD"),
        "MONGO_URI":             os.getenv("MONGO_URI"),
        "AWS_BUCKET":            os.getenv("AWS_BUCKET"),
        "AWS_REGION":            os.getenv("AWS_REGION", "ap-south-1"),
        "AWS_ACCESS_KEY_ID":     os.getenv("AWS_ACCESS_KEY_ID"),
        "AWS_SECRET_ACCESS_KEY": os.getenv("AWS_SECRET_ACCESS_KEY"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        fail(f"Missing environment variables: {', '.join(missing)}")
        print(f"\n  Copy {BOLD}.env.example{RESET} to {BOLD}.env{RESET} and fill in your details.")
        print(f"  Then run the script again.\n")
        sys.exit(1)
    return required

# ─────────────────────────────────────────────
#  Startup banner
# ─────────────────────────────────────────────
def print_banner(cfg):
    print(f"\n{BOLD}{CYAN}{'='*54}{RESET}")
    print(f"{BOLD}{CYAN}   MongoDB Automated Backup System  v2.0{RESET}")
    print(f"{BOLD}{CYAN}{'='*54}{RESET}")
    print(f"  {BOLD}MongoDB:{RESET}  {cfg['MONGO_URI']}")
    print(f"  {BOLD}S3 Bucket:{RESET} {cfg['AWS_BUCKET']}  ({cfg['AWS_REGION']})")
    print(f"  {BOLD}Schedule:{RESET} Daily at 02:00 AM  |  Keeps last 7 backups")
    print(f"  {BOLD}Logs:{RESET}     logs/backup.log")
    print(f"{BOLD}{CYAN}{'='*54}{RESET}\n")

# ─────────────────────────────────────────────
#  MongoDB connection (with test)
# ─────────────────────────────────────────────
def connect_db(cfg):
    try:
        username = quote_plus(cfg["MONGO_USERNAME"])
        password = quote_plus(cfg["MONGO_PASSWORD"])
        uri = f"mongodb+srv://{username}:{password}@{cfg['MONGO_URI']}/?appName=Cluster0"
        client = MongoClient(uri, server_api=ServerApi("1"), serverSelectionTimeoutMS=10000)
        client.admin.command("ping")   # test the connection
        ok("Connected to MongoDB Atlas")
        log.info("Connected to MongoDB Atlas")
        return client
    except Exception as e:
        fail(f"Could not connect to MongoDB: {e}")
        log.error(f"MongoDB connection failed: {e}")
        return None

# ─────────────────────────────────────────────
#  S3 upload (with retry)
# ─────────────────────────────────────────────
def upload_to_s3(file_path, cfg, retries=3):
    s3 = boto3.client(
        "s3",
        region_name=cfg["AWS_REGION"],
        aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"],
    )
    file_name = os.path.basename(file_path)
    s3_key = f"backups/{file_name}"

    for attempt in range(1, retries + 1):
        try:
            info(f"Uploading to S3 (attempt {attempt}/{retries})...")
            s3.upload_file(file_path, cfg["AWS_BUCKET"], s3_key)
            ok(f"Uploaded → s3://{cfg['AWS_BUCKET']}/{s3_key}")
            log.info(f"Uploaded to S3: {s3_key}")
            return True
        except NoCredentialsError:
            fail("AWS credentials not found. Check AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY in .env")
            log.error("AWS credentials missing")
            return False
        except ClientError as e:
            warn(f"S3 error on attempt {attempt}: {e}")
            log.warning(f"S3 upload attempt {attempt} failed: {e}")
            if attempt < retries:
                time.sleep(5)
    fail("S3 upload failed after all retries.")
    log.error("S3 upload failed after all retries")
    return False

# ─────────────────────────────────────────────
#  Main backup function
# ─────────────────────────────────────────────
def backup_database(cfg):
    start_time = datetime.datetime.now()
    print(f"\n{BOLD}{'─'*54}{RESET}")
    info(f"Backup started at {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log.info("Backup started")

    # Connect
    client = connect_db(cfg)
    if not client:
        fail("Skipping backup due to connection failure.")
        return False

    # Discover databases
    try:
        db_names = client.list_database_names()
    except Exception as e:
        fail(f"Could not list databases: {e}")
        log.error(f"list_database_names failed: {e}")
        client.close()
        return False

    # Create local backup folder
    timestamp = start_time.strftime("%Y%m%d_%H%M%S")
    backup_folder = f"backup_{timestamp}"
    os.makedirs(backup_folder, exist_ok=True)

    total_docs = 0
    skipped_dbs = ["admin", "local", "config"]

    for db_name in db_names:
        if db_name in skipped_dbs:
            continue
        db = client[db_name]
        collections = db.list_collection_names()
        info(f"Database: {BOLD}{db_name}{RESET}  ({len(collections)} collections)")
        db_folder = os.path.join(backup_folder, db_name)
        os.makedirs(db_folder, exist_ok=True)

        for col_name in collections:
            try:
                col = db[col_name]
                documents = list(col.find({}, {"_id": 0}))
                file_path = os.path.join(db_folder, f"{col_name}.json")
                with open(file_path, "w") as f:
                    json.dump(documents, f, indent=2, default=str)
                total_docs += len(documents)
                ok(f"  {col_name}: {len(documents):,} documents")
                log.info(f"  {db_name}.{col_name}: {len(documents)} docs")
            except Exception as e:
                warn(f"  {col_name}: failed — {e}")
                log.warning(f"  {db_name}.{col_name} backup failed: {e}")

    client.close()

    # Zip it
    zip_filename = f"{backup_folder}.zip"
    try:
        with zipfile.ZipFile(zip_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(backup_folder):
                for file in files:
                    zipf.write(os.path.join(root, file))
        shutil.rmtree(backup_folder)
        size_mb = os.path.getsize(zip_filename) / (1024 * 1024)
        ok(f"Zipped → {zip_filename}  ({size_mb:.2f} MB)")
        log.info(f"Zip created: {zip_filename} ({size_mb:.2f} MB)")
    except Exception as e:
        fail(f"Failed to create zip: {e}")
        log.error(f"Zip creation failed: {e}")
        return False

    # Upload to S3
    uploaded = upload_to_s3(zip_filename, cfg)

    # Cleanup old local backups (keep last 7)
    backups = sorted([f for f in os.listdir(".") if f.startswith("backup_") and f.endswith(".zip")])
    if len(backups) > 7:
        for old in backups[:-7]:
            os.remove(old)
            warn(f"Deleted old local backup: {old}")
            log.info(f"Deleted old backup: {old}")

    elapsed = (datetime.datetime.now() - start_time).seconds
    print(f"\n{GREEN}{BOLD}  Backup complete!{RESET}  {total_docs:,} documents  |  {elapsed}s elapsed")
    print(f"{BOLD}{'─'*54}{RESET}\n")
    log.info(f"Backup complete: {total_docs} docs, {elapsed}s, S3={'OK' if uploaded else 'FAILED'}")
    return uploaded

# ─────────────────────────────────────────────
#  Scheduler
# ─────────────────────────────────────────────
def run_scheduler():
    cfg = load_config()
    print_banner(cfg)

    # Run once immediately on start
    backup_database(cfg)

    # Schedule daily at 2 AM
    schedule.every().day.at("02:00").do(backup_database, cfg)
    info("Scheduler running. Next backup at 02:00 AM daily.")
    info("Press Ctrl+C to stop.\n")

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    try:
        run_scheduler()
    except KeyboardInterrupt:
        print(f"\n{YELLOW}  Stopped by user.{RESET}\n")
