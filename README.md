# HROne Attendance API

Run:
```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # set MONGO_URI, MONGO_DB
python sample_seed.py
uvicorn app.main:app --port 8000
```

All code in app/main.py, indexes created at startup, env vars priority.

Endpoints: /health, /employees, /attendance/punch-in, /attendance/punch-out, /attendance, PATCH /attendance/{emp_code}/{date}, /analytics/employees/{emp_code}/monthly, /analytics/departments/summary, /analytics/leaderboard/late, /analytics/departments/{department}/trend, /admin/explain/{endpoint}
