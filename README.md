# MongoDB Backup System

Automated MongoDB database backup system with a web-based setup wizard.
Backs up all collections to JSON, compresses to ZIP, uploads to AWS S3, and runs on a daily schedule.

Built as a POC (Proof of Concept) for Cloud Counselage GPI internship.

---

## Features

- Web-based setup wizard — no need to edit config files manually
- Credentials encrypted and stored locally on your machine
- Connects to MongoDB Atlas and backs up all collections to JSON
- Compresses each backup to ZIP and uploads to AWS S3
- Retries S3 upload up to 3 times on failure
- Keeps only the last 7 local backups (auto-cleanup)
- Live dashboard showing last run, status, and history
- Run backup manually anytime via the dashboard
- Saves a full log to logs/backup.log

---

## Setup

1. Install dependencies:
   pip install -r requirements.txt

2. Run the app:
   python app.py

3. Open your browser:
   http://localhost:5000

The setup wizard guides you through entering credentials once.
They are encrypted and saved locally on your machine.

---

## Project Info

- Institution: Vidyalankar Institute of Technology, Mumbai
- Internship: Cloud Counselage Pvt. Ltd. (GPI)
- Domain: Cloud Computing
- Student: Dhruv Patil — Roll No: 23102A0026 — Intern ID: IP-11448
