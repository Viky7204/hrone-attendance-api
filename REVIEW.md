# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | get_db() | MongoClient created per request, MONGO_URI hard-coded, no env priority | Check env var ignored, connection leak under load | Singleton client, read MONGO_URI/MONGO_DB from env with dotenv, tz_aware=True |
| 2 | /health | Returns 200 even if Mongo down, no ping | Stop Mongo, health still 200 | db.command('ping'), 503 on failure |
| 3 | punch-in timestamp validation | Accepts seconds (e.g. 1710000000), float, string, not 422 | Send {"punched_at": 1710000000} should be 422 but was 201 | is_epoch_millis check 100bn-4.1tn, trunc to sec |
| 4 | R1 timezone | Uses UTC date for attendance date, not IST, overnight shift not handled | Shift 22:00-06:00 punch 01:00 IST should belong to previous day but creates new date | Convert to IST naive, if et<=st and punch time < et -> date-1 |
| 5 | concurrency punch-in | Check-then-insert, two parallel punch-ins both 201 | Run 2 parallel POST same emp/date -> duplicate docs | Unique index (emp_code,date) + catch DuplicateKeyError ->409 |
| 6 | late calculation R2 | Calculates from grace end, uses ceil, not floor since shift_start | Shift 09:30 punch 09:40:01 should give 10 but gave 0 | If diff>600 sec, late=floor(diff/60) |
| 7 | overtime R3 | Counts any minutes after shift_end, no 30min threshold | Punch out 18:35 with 5 min over should be 0 but gave 5 | floor and >=30 else 0 |
| 8 | work_hours rounding R4 R8 | Uses Python round (bankers) | 9.125 rounds to 9.12 not 9.13 | Decimal quantize ROUND_HALF_UP 2 decimals |
| 9 | pagination R10 | page starts at 0, no max page_size, total not filtered | ?page=0 or page_size=1000 accepted | Query ge=1 le=100 default 20, count_documents with same filter |
| 10 | leaking _id | Responses return Mongo _id | GET /attendance returns _id field | Projection excludes _id, to_api_record converts dates to millis |
| 11 | /employees duplicate | Returns 500 on duplicate emp_code instead of 409 | POST same emp_code twice ->500 | Catch DuplicateKeyError ->409, unique index |
| 12 | punch-out logic | Uses emp_code+date query, fails for overnight shift, no 24h check, no race safety | Overnight shift punch-out on next calendar day 404, concurrent punch-outs both 200 | Find most recent punch_in <= punched_at, validate >punch_in and <=24h, update with punch_out=None filter ->409 |
| 13 | regularize history loss | Overwrites history array, loses concurrent edits | Two PATCH parallel second loses first history | $push history atomically, compute changes only actually changed, 422 if no change, validate ABSENT/LEAVE clears times |
| 14 | indexes | No indexes at startup, will COLLSCAN at 100k | Explain shows COLLSCAN | Create all indexes at startup idempotently |

Not defects:
- Using PyMongo sync in FastAPI is okay for this assignment; Motor would be alternative but not required.
- Storing BSON dates as UTC and converting to IST at boundary is intended.
