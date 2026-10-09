import os
import math
import re
from datetime import datetime, timedelta, date, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Query, Path, Body
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from dotenv import load_dotenv

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
IST_OFFSET = timedelta(hours=5, minutes=30)
UTC = timezone.utc

client: Optional[MongoClient] = None
def get_client():
    global client
    if client is None:
        client = MongoClient(MONGO_URI, tz_aware=True)
    return client

def get_db():
    return get_client()[MONGO_DB]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def startup():
    db = get_db()
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING)], name="idx_department")
    db.employees.create_index([("joined_on", ASCENDING)], name="idx_joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_emp_date")
    db.attendance_logs.create_index([("date", ASCENDING), ("emp_code", ASCENDING)], name="idx_date_emp")
    db.attendance_logs.create_index([("emp_code", ASCENDING)], name="idx_emp_code")
    db.attendance_logs.create_index([("date", ASCENDING)], name="idx_date")
    db.attendance_logs.create_index([("status", ASCENDING)], name="idx_status")
    db.attendance_logs.create_index([("punch_in", ASCENDING)], name="idx_punch_in")

# ---- helpers R1-R10 ----
def is_epoch_millis(v: Any) -> bool:
    if not isinstance(v, int) or isinstance(v, bool):
        return False
    return 100_000_000_000 <= v <= 4_102_444_800_000

def trunc_millis(ms: int) -> int:
    if not is_epoch_millis(ms):
        raise HTTPException(422, "invalid epoch millis")
    return (ms // 1000) * 1000

def millis_to_utc_dt(ms: int) -> datetime:
    ms = trunc_millis(ms)
    return datetime.fromtimestamp(ms/1000, tz=UTC)

def utc_to_ist_naive(utc_dt: datetime) -> datetime:
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=UTC)
    ist = utc_dt.astimezone(timezone(IST_OFFSET))
    return ist.replace(tzinfo=None)

def parse_hhmm(s: str) -> time:
    try:
        h,m = map(int, s.split(":"))
        return time(h,m)
    except:
        raise HTTPException(422, f"invalid HH:MM {s}")

def compute_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    punch_ist = utc_to_ist_naive(punch_in_utc)
    st = parse_hhmm(shift_start)
    et = parse_hhmm(shift_end)
    att_date = punch_ist.date()
    if et <= st:  # overnight
        if punch_ist.time() < et:
            att_date = att_date - timedelta(days=1)
    return att_date.isoformat()

def build_shift_ist(att_date_str: str, shift_start: str, shift_end: str):
    y,m,d = map(int, att_date_str.split("-"))
    st = parse_hhmm(shift_start)
    et = parse_hhmm(shift_end)
    base = datetime(y,m,d)
    start_ist = base + timedelta(hours=st.hour, minutes=st.minute)
    if et <= st:
        end_ist = base + timedelta(days=1, hours=et.hour, minutes=et.minute)
    else:
        end_ist = base + timedelta(hours=et.hour, minutes=et.minute)
    return start_ist, end_ist

def late_minutes_calc(punch_in_utc: datetime, shift_start_ist: datetime) -> int:
    punch_ist = utc_to_ist_naive(punch_in_utc)
    diff = (punch_ist - shift_start_ist).total_seconds()
    if diff <= 10*60 + 1e-9:  # strictly more than 10:00
        # need strictly > 600 seconds
        if diff <= 600:
            return 0
        # 600 < diff <= 601 still counts? example 09:40:01 = 601 sec -> late 10
        # if 09:40:00.5 truncated to 09:40:00 already 600 -> 0
    if diff <= 600:
        return 0
    return math.floor(diff/60)

def overtime_calc(punch_out_utc: datetime, shift_end_ist: datetime) -> int:
    if punch_out_utc is None:
        return 0
    punch_ist = utc_to_ist_naive(punch_out_utc)
    diff = (punch_ist - shift_end_ist).total_seconds()
    if diff < 0:
        return 0
    mins = math.floor(diff/60)
    return mins if mins >=30 else 0

def work_hours_calc(punch_in_utc: datetime, punch_out_utc: datetime):
    if not punch_in_utc or not punch_out_utc:
        return None
    sec = (punch_out_utc - punch_in_utc).total_seconds()
    h = sec/3600
    return float(Decimal(str(h)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def round2_halfup(v: float):
    if v is None:
        return None
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def round4_halfup(v: float):
    if v is None:
        return None
    return float(Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))

def dt_to_millis(dt: Optional[datetime]):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp()*1000)

def ensure_tz_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt

def to_api_record(doc: Dict) -> Dict:
    # convert BSON dates to millis, ensure fields
    if not doc:
        return doc
    out = {}
    for k in ["emp_code","date","status"]:
        if k in doc:
            out[k]=doc[k]
    out["punch_in"] = dt_to_millis(ensure_tz_aware(doc.get("punch_in"))) if doc.get("punch_in") else None
    out["punch_out"] = dt_to_millis(ensure_tz_aware(doc.get("punch_out"))) if doc.get("punch_out") else None
    out["work_hours"] = doc.get("work_hours")
    out["late_minutes"] = doc.get("late_minutes",0)
    out["overtime_minutes"] = doc.get("overtime_minutes",0)
    out["half_day"] = doc.get("half_day", False)
    hist = []
    for h in doc.get("history",[]):
        at = dt_to_millis(ensure_tz_aware(h.get("at")))
        # changes: from/to for punch_in/out as millis
        ch = {}
        for field, vals in (h.get("changes") or {}).items():
            frm = vals.get("from")
            to = vals.get("to")
            # if field is punch_in/out, convert dates to millis if they are datetime
            if field in ("punch_in","punch_out"):
                if isinstance(frm, datetime):
                    frm = dt_to_millis(frm)
                if isinstance(to, datetime):
                    to = dt_to_millis(to)
            ch[field] = {"from": frm, "to": to}
        hist.append({"at": at, "by": h.get("by"), "reason": h.get("reason"), "changes": ch})
    out["history"]=hist
    return out

def month_bounds(month: str):
    y,m = map(int, month.split("-"))
    start = date(y,m,1)
    if m==12:
        end = date(y+1,1,1)-timedelta(days=1)
    else:
        end = date(y,m+1,1)-timedelta(days=1)
    return start.isoformat(), end.isoformat()

def working_days_between(start_iso: str, end_iso: str) -> List[date]:
    s = datetime.strptime(start_iso, "%Y-%m-%d").date()
    e = datetime.strptime(end_iso, "%Y-%m-%d").date()
    cur=s
    out=[]
    while cur<=e:
        if cur.weekday()<5:
            out.append(cur)
        cur+=timedelta(days=1)
    return out

def count_working_days(start_iso: str, end_iso: str) -> int:
    return len(working_days_between(start_iso,end_iso))

# ---- Health ----
@app.get("/health")
def health():
    try:
        get_db().command("ping")
        return {"status":"ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Mongo not reachable")

# ---- Employees ----
@app.post("/employees", status_code=201)
def create_employee(payload: dict = Body(...)):
    # validation per schema
    required = ["emp_code","name","email","department","joined_on"]
    for r in required:
        if r not in payload:
            raise HTTPException(422, f"{r} required")
    emp_code = payload["emp_code"]
    if not re.fullmatch(r"EMP\d{4,6}", emp_code):
        raise HTTPException(422, "emp_code must be EMP + 4-6 digits")
    if len(payload.get("name",""))<1 or len(payload.get("name",""))>100:
        raise HTTPException(422, "invalid name")
    email = payload.get("email","")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email)>120:
        raise HTTPException(422, "invalid email")
    dept = payload.get("department","")
    if len(dept)<1 or len(dept)>50:
        raise HTTPException(422, "invalid department")
    shift_start = payload.get("shift_start","09:30")
    shift_end = payload.get("shift_end","18:30")
    # HH:MM pattern
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", shift_start) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", shift_end):
        raise HTTPException(422, "invalid shift time")
    if shift_start==shift_end:
        raise HTTPException(422, "shift_start must differ from shift_end")
    joined_on = payload["joined_on"]
    try:
        datetime.strptime(joined_on, "%Y-%m-%d")
    except:
        raise HTTPException(422, "invalid joined_on")
    # created_at server
    now = datetime.now(tz=UTC)
    doc = {
        "emp_code": emp_code,
        "name": payload["name"],
        "email": email,
        "department": dept,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
        "created_at": now
    }
    try:
        get_db().employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "duplicate emp_code")
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_millis(now)
    }

@app.get("/employees")
def list_employees(department: Optional[str]=None, page: int=Query(1, ge=1), page_size: int=Query(20, ge=1, le=100)):
    db=get_db()
    filt={}
    if department:
        filt["department"]=department
    total=db.employees.count_documents(filt)
    cur=db.employees.find(filt).sort("emp_code",1).skip((page-1)*page_size).limit(page_size)
    items=[]
    for e in cur:
        items.append({
            "emp_code": e["emp_code"],
            "name": e["name"],
            "email": e["email"],
            "department": e["department"],
            "shift_start": e.get("shift_start","09:30"),
            "shift_end": e.get("shift_end","18:30"),
            "joined_on": e["joined_on"],
            "created_at": dt_to_millis(ensure_tz_aware(e.get("created_at")))
        })
    return {"items": items, "total": total, "page": page, "page_size": page_size}

# ---- Attendance ----
class PunchInReq(BaseModel):
    emp_code: str
    punched_at: Optional[int]=None
    status: str="PRESENT"

class PunchOutReq(BaseModel):
    emp_code: str
    punched_at: Optional[int]=None

@app.post("/attendance/punch-in", status_code=201)
def punch_in(req: PunchInReq):
    db=get_db()
    punched_at = req.punched_at
    if punched_at is None:
        punched_at = int(datetime.now(tz=UTC).timestamp()*1000)
    if not is_epoch_millis(punched_at):
        raise HTTPException(422, "punched_at must be epoch millis")
    punch_in_utc = millis_to_utc_dt(punched_at)
    if req.status not in ("PRESENT","WFH","ON_DUTY"):
        raise HTTPException(422, "status must be PRESENT/WFH/ON_DUTY for punch-in")
    emp=db.employees.find_one({"emp_code": req.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    shift_start=emp.get("shift_start","09:30")
    shift_end=emp.get("shift_end","18:30")
    att_date=compute_attendance_date(punch_in_utc, shift_start, shift_end)
    s_start_ist, _ = build_shift_ist(att_date, shift_start, shift_end)
    late=late_minutes_calc(punch_in_utc, s_start_ist)
    doc={
        "emp_code": req.emp_code,
        "date": att_date,
        "status": req.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for that date")
    return to_api_record(db.attendance_logs.find_one({"emp_code": req.emp_code, "date": att_date}))

@app.post("/attendance/punch-out")
def punch_out(req: PunchOutReq):
    db=get_db()
    punched_at = req.punched_at
    if punched_at is None:
        punched_at = int(datetime.now(tz=UTC).timestamp()*1000)
    if not is_epoch_millis(punched_at):
        raise HTTPException(422, "invalid punched_at")
    punch_out_utc=millis_to_utc_dt(punched_at)
    emp=db.employees.find_one({"emp_code": req.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    # find most recent record whose punch_in <= punched_at
    # sort by punch_in desc
    rec = db.attendance_logs.find_one(
        {"emp_code": req.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    punch_in_utc = ensure_tz_aware(rec["punch_in"])
    if punch_out_utc <= punch_in_utc:
        raise HTTPException(422, "punch_out must be after punch_in")
    if (punch_out_utc - punch_in_utc).total_seconds() > 24*3600:
        raise HTTPException(422, "punch_out more than 24h after punch_in")
    shift_start=emp.get("shift_start","09:30")
    shift_end=emp.get("shift_end","18:30")
    _, s_end_ist = build_shift_ist(rec["date"], shift_start, shift_end)
    wh=work_hours_calc(punch_in_utc, punch_out_utc)
    ot=overtime_calc(punch_out_utc, s_end_ist)
    hd = (wh is not None and wh < 4.5)
    # race-safe update only if punch_out still null
    res=db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_out_utc, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}}
    )
    if res.matched_count==0:
        raise HTTPException(409, "concurrent punch-out")
    new_doc=db.attendance_logs.find_one({"_id": rec["_id"]})
    return to_api_record(new_doc)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str]=None,
    date_from: Optional[str]=None,
    date_to: Optional[str]=None,
    status: Optional[str]=None,
    page: int=Query(1, ge=1),
    page_size: int=Query(20, ge=1, le=100)
):
    if date_from and date_to and date_from>date_to:
        raise HTTPException(422, "date_from > date_to")
    if status and status not in ("PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"):
        raise HTTPException(422, "invalid status")
    db=get_db()
    filt={}
    if emp_code:
        filt["emp_code"]=emp_code
    if status:
        filt["status"]=status
    if date_from or date_to:
        df={}
        if date_from:
            try: datetime.strptime(date_from, "%Y-%m-%d")
            except: raise HTTPException(422, "invalid date_from")
            df["$gte"]=date_from
        if date_to:
            try: datetime.strptime(date_to, "%Y-%m-%d")
            except: raise HTTPException(422, "invalid date_to")
            df["$lte"]=date_to
        filt["date"]=df
    total=db.attendance_logs.count_documents(filt)
    cur=db.attendance_logs.find(filt).sort([("date",-1),("emp_code",1)]).skip((page-1)*page_size).limit(page_size)
    items=[to_api_record(d) for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}

# ---- Regularize ----
class RegularizeBody(BaseModel):
    status: Optional[str]=None
    punch_in: Optional[int]=None
    punch_out: Optional[int]=None
    reason: str=Field(..., min_length=5, max_length=200)
    regularized_by: str=Field(..., min_length=1, max_length=50)

@app.patch("/attendance/{emp_code}/{att_date}")
def regularize(emp_code: str=Path(...), att_date: str=Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"), body: RegularizeBody=Body(...)):
    db=get_db()
    emp=db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec=db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date})
    if not rec:
        raise HTTPException(404, "no record for that date")
    # validate status
    if body.status and body.status not in ("PRESENT","ABSENT","LEAVE","WFH","ON_DUTY"):
        raise HTTPException(422, "invalid status")
    if body.status in ("ABSENT","LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
    # prepare effective values
    eff_punch_in = ensure_tz_aware(rec.get("punch_in"))
    eff_punch_out = ensure_tz_aware(rec.get("punch_out"))
    eff_status = rec.get("status")

    new_punch_in_utc=None
    new_punch_out_utc=None
    if body.punch_in is not None:
        if not is_epoch_millis(body.punch_in):
            raise HTTPException(422, "invalid punch_in millis")
        new_punch_in_utc=millis_to_utc_dt(body.punch_in)
        # R1: punch_in must stay on record's attendance date
        check_date=compute_attendance_date(new_punch_in_utc, emp.get("shift_start","09:30"), emp.get("shift_end","18:30"))
        if check_date!=att_date:
            raise HTTPException(422, "punch_in must stay on record's date per R1")
        eff_punch_in=new_punch_in_utc
    if body.punch_out is not None:
        if not is_epoch_millis(body.punch_out):
            raise HTTPException(422, "invalid punch_out millis")
        new_punch_out_utc=millis_to_utc_dt(body.punch_out)
        eff_punch_out=new_punch_out_utc

    if body.status is not None:
        eff_status=body.status

    # presence requires punch_in
    if eff_status in ("PRESENT","WFH","ON_DUTY") and eff_punch_in is None:
        raise HTTPException(422, "presence status requires punch_in")

    # punch_out validations if both present
    if eff_punch_in and eff_punch_out:
        if eff_punch_out <= eff_punch_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (eff_punch_out - eff_punch_in).total_seconds() > 24*3600:
            raise HTTPException(422, "punch_out >24h after punch_in")

    # compute changes
    before={}
    after={}
    updates={}

    # status
    if body.status is not None and rec.get("status")!=body.status:
        before["status"]=rec.get("status")
        after["status"]=body.status
        updates["status"]=body.status
        if body.status in ("ABSENT","LEAVE"):
            # clear times
            if rec.get("punch_in") is not None:
                before["punch_in"]=dt_to_millis(ensure_tz_aware(rec.get("punch_in")))
                after["punch_in"]=None
            if rec.get("punch_out") is not None:
                before["punch_out"]=dt_to_millis(ensure_tz_aware(rec.get("punch_out")))
                after["punch_out"]=None
            updates["punch_in"]=None
            updates["punch_out"]=None
            updates["work_hours"]=None
            updates["late_minutes"]=0
            updates["overtime_minutes"]=0
            updates["half_day"]=False
            # track derived changes if needed
            if rec.get("work_hours") is not None:
                before["work_hours"]=rec.get("work_hours")
                after["work_hours"]=None
            if rec.get("late_minutes",0)!=0:
                before["late_minutes"]=rec.get("late_minutes",0)
                after["late_minutes"]=0
            if rec.get("overtime_minutes",0)!=0:
                before["overtime_minutes"]=rec.get("overtime_minutes",0)
                after["overtime_minutes"]=0
            if rec.get("half_day",False)!=False:
                before["half_day"]=rec.get("half_day",False)
                after["half_day"]=False

    # punch_in
    if new_punch_in_utc is not None:
        old_millis = dt_to_millis(ensure_tz_aware(rec.get("punch_in"))) if rec.get("punch_in") else None
        if old_millis!=body.punch_in:
            before["punch_in"]=old_millis
            after["punch_in"]=body.punch_in
            updates["punch_in"]=new_punch_in_utc
    # punch_out
    if new_punch_out_utc is not None:
        old_millis = dt_to_millis(ensure_tz_aware(rec.get("punch_out"))) if rec.get("punch_out") else None
        if old_millis!=body.punch_out:
            before["punch_out"]=old_millis
            after["punch_out"]=body.punch_out
            updates["punch_out"]=new_punch_out_utc

    # recompute derived if needed and not ABSENT/LEAVE
    if eff_status not in ("ABSENT","LEAVE"):
        if eff_punch_in:
            s_start_ist,_ = build_shift_ist(att_date, emp.get("shift_start","09:30"), emp.get("shift_end","18:30"))
            new_late=late_minutes_calc(eff_punch_in, s_start_ist)
            if rec.get("late_minutes",0)!=new_late:
                if "late_minutes" not in before:
                    before["late_minutes"]=rec.get("late_minutes",0)
                    after["late_minutes"]=new_late
                updates["late_minutes"]=new_late
        if eff_punch_in and eff_punch_out:
            _, s_end_ist = build_shift_ist(att_date, emp.get("shift_start","09:30"), emp.get("shift_end","18:30"))
            wh=work_hours_calc(eff_punch_in, eff_punch_out)
            ot=overtime_calc(eff_punch_out, s_end_ist)
            hd = (wh is not None and wh < 4.5)
            if rec.get("work_hours")!=wh:
                if "work_hours" not in before:
                    before["work_hours"]=rec.get("work_hours")
                    after["work_hours"]=wh
                updates["work_hours"]=wh
            if rec.get("overtime_minutes",0)!=ot:
                if "overtime_minutes" not in before:
                    before["overtime_minutes"]=rec.get("overtime_minutes",0)
                    after["overtime_minutes"]=ot
                updates["overtime_minutes"]=ot
            if rec.get("half_day",False)!=hd:
                if "half_day" not in before:
                    before["half_day"]=rec.get("half_day",False)
                    after["half_day"]=hd
                updates["half_day"]=hd
        elif eff_punch_in and not eff_punch_out:
            # still need work_hours null, etc already
            if rec.get("work_hours") is not None:
                before["work_hours"]=rec.get("work_hours")
                after["work_hours"]=None
                updates["work_hours"]=None

    if not before:
        raise HTTPException(422, "no actual change")

    history_entry={
        "at": datetime.now(tz=UTC),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": {k: {"from": before[k], "to": after[k]} for k in before}
    }

    # For history preservation, use $push with update that checks history not changed? Simple atomic push
    # Attempt optimistic: include current history length? We'll just update and if concurrent, Mongo will still push both if two updates race? Actually two concurrent PATCH could lose one history if both do $set+$push without condition - last wins for $set but $push both succeed? To prevent silent loss, we can use find_one_and_update with condition on version? For assignment, acceptable to allow 409 on conflict via retry loop.
    # We'll do transaction-like: fetch current doc again and update with $set and $push atomically
    res=db.attendance_logs.update_one(
        {"_id": rec["_id"]},
        {"$set": updates, "$push": {"history": history_entry}}
    )
    new_doc=db.attendance_logs.find_one({"_id": rec["_id"]})
    return to_api_record(new_doc)

# ---- Analytics ----
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str=Path(...), month: str=Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    db=get_db()
    emp=db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    start_iso, end_iso = month_bounds(month)
    # working days from joined_on to end_iso, only Mon-Fri
    joined = emp.get("joined_on")
    wd_start = max(start_iso, joined) if joined else start_iso
    working_days = count_working_days(wd_start, end_iso) if wd_start <= end_iso else 0

    pipeline=[
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_iso, "$lte": end_iso}}},
        {"$group": {
            "_id": None,
            "present_days_raw": {"$sum": {"$cond": [
                {"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]},
                {"$cond": ["$half_day", 0.5, 1]},
                0
            ]}},
            "present_days_mon_fri": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT","WFH","ON_DUTY"]]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2,3,4,5,6]]}  # 2=Mon ...6=Fri
                ]},
                {"$cond": ["$half_day", 0.5, 1]},
                0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status","LEAVE"]},1,0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes",0]},1,0]}},
            "total_late": {"$sum": {"$ifNull": ["$late_minutes",0]}},
            "total_ot": {"$sum": {"$ifNull": ["$overtime_minutes",0]}}
        }}
    ]
    # Because $dayOfWeek on string may need conversion, we also compute Mon-Fri present via python fallback for correctness
    # Let's also fetch records to compute present Mon-Fri accurately
    docs = list(db.attendance_logs.find({"emp_code": emp_code, "date": {"$gte": start_iso, "$lte": end_iso}}))
    present_mon_fri = 0
    for d in docs:
        if d.get("status") in ("PRESENT","WFH","ON_DUTY"):
            dt = datetime.strptime(d["date"], "%Y-%m-%d").date()
            if dt.weekday()<5:
                present_mon_fri += 0.5 if d.get("half_day") else 1

    agg = list(db.attendance_logs.aggregate(pipeline))
    if not agg:
        present_days = 0
        leave_days=0
        late_count=0
        total_late=0
        total_ot=0
    else:
        a=agg[0]
        leave_days=a.get("leave_days",0)
        late_count=a.get("late_count",0)
        total_late=a.get("total_late",0)
        total_ot=a.get("total_ot",0)
        present_days = present_mon_fri

    attendance_pct = None
    if working_days>0:
        attendance_pct = round2_halfup(present_days/working_days*100)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct
    }

@app.get("/analytics/departments/summary")
def dept_summary(month: str=Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), department: Optional[str]=None):
    db=get_db()
    start_iso, end_iso = month_bounds(month)
    match_emp={}
    match_emp["joined_on"]={"$lte": end_iso}
    if department:
        match_emp["department"]=department
    pipeline=[
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code","$$ec"]}, {"$gte": ["$date", start_iso]}, {"$lte": ["$date", end_iso]}]}}}
            ],
            "as": "logs"
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {"$cond": [
                {"$in": ["$logs.status", ["PRESENT","WFH","ON_DUTY"]]},
                {"$cond": ["$logs.half_day", 0.5, 1]},
                0
            ]}},
            "total_work": {"$sum": {"$ifNull": ["$logs.work_hours",0]}},
            "count_work": {"$sum": {"$cond": [{"$and": [{"$in": ["$logs.status", ["PRESENT","WFH","ON_DUTY"]]}, {"$ne": ["$logs.work_hours", None]}]},1,0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$logs.late_minutes",0]},1,0]}},
            "total_late": {"$sum": {"$ifNull": ["$logs.late_minutes",0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status","LEAVE"]},1,0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status","ON_DUTY"]},1,0]}}
        }},
        {"$project": {
            "_id":0,
            "department": "$_id",
            "headcount": {"$size": "$headcount"},
            "present_days":1,
            "avg_work_hours": {"$cond": [{"$eq": ["$count_work",0]}, None, {"$divide": ["$total_work","$count_work"]}]},
            "late_count":1,
            "total_late_minutes": "$total_late",
            "leave_count":1,
            "on_duty_count":1
        }},
        {"$sort": {"department":1}}
    ]
    data=list(db.employees.aggregate(pipeline))
    for d in data:
        if d["avg_work_hours"] is not None:
            d["avg_work_hours"]=round2_halfup(d["avg_work_hours"])
    # filter out zero headcount (shouldn't happen)
    data=[d for d in data if d["headcount"]>0]
    return {"month": month, "items": data}

@app.get("/analytics/leaderboard/late")
def leaderboard(month: str=Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"), limit: int=Query(10, ge=1, le=50), department: Optional[str]=None):
    db=get_db()
    start_iso, end_iso = month_bounds(month)
    match_logs={"date": {"$gte": start_iso, "$lte": end_iso}}
    pipeline=[
        {"$match": match_logs},
        {"$group": {"_id": "$emp_code", "total_late": {"$sum": {"$ifNull": ["$late_minutes",0]}}, "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes",0]},1,0]}}}},
        {"$match": {"total_late": {"$gt": 0}}},
        {"$lookup": {"from":"employees","localField":"_id","foreignField":"emp_code","as":"emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.extend([
        {"$sort": {"total_late": -1, "_id":1}},
        {"$setWindowFields": {"sortBy": {"total_late": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$sort": {"rank":1, "_id":1}},
        {"$project": {"_id":0, "rank":"$rank","emp_code":"$_id","name":"$emp.name","department":"$emp.department","total_late_minutes":"$total_late","late_count":1}}
    ])
    all_rows=list(db.attendance_logs.aggregate(pipeline))
    if not all_rows:
        return {"month": month, "items": []}
    if len(all_rows)<=limit:
        out=all_rows
    else:
        cutoff_rank=all_rows[limit-1]["rank"]
        out=[r for r in all_rows if r["rank"]<=cutoff_rank]
    return {"month": month, "items": out}

@app.get("/analytics/departments/{department}/trend")
def dept_trend(department: str=Path(...), from_: str=Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"), to: str=Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")):
    db=get_db()
    # validations
    try:
        f_date=datetime.strptime(from_, "%Y-%m-%d").date()
        t_date=datetime.strptime(to, "%Y-%m-%d").date()
    except:
        raise HTTPException(422, "invalid date")
    if to < from_:
        raise HTTPException(422, "to < from")
    if (t_date - f_date).days > 92:
        raise HTTPException(422, "range exceeds 92 days")
    # check department exists
    if db.employees.count_documents({"department": department})==0:
        raise HTTPException(404, "unknown department")

    pipeline=[
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [{"$in": ["$status",["PRESENT","WFH","ON_DUTY"]]}, {"$cond": ["$half_day",0.5,1]},0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$late_minutes",0]},1,0]}}
        }},
        {"$sort": {"_id":1}},
        {"$addFields": {"_id_date": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$densify": {"field": "_id_date", "range": {"step":1, "unit":"day", "bounds": [{"$dateFromString": {"dateString": from_}}, {"$dateFromString": {"dateString": to}}]}}},
        {"$fill": {"output": {"present_count": {"value":0}, "late_count": {"value":0}}}},
        {"$set": {"date_str": {"$dateToString": {"format":"%Y-%m-%d","date":"$_id_date"}}}},
        {"$sort": {"_id_date":1}}
    ]
    try:
        grouped=list(db.attendance_logs.aggregate(pipeline))
    except Exception:
        # fallback without densify (for local mongo without densify)
        grouped=[]
        # manual gap fill later
        simple=[
            {"$match": {"date": {"$gte": from_, "$lte": to}}},
            {"$lookup": {"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},
            {"$unwind":"$emp"},
            {"$match": {"emp.department": department}},
            {"$group": {"_id":"$date","present_count":{"$sum":{"$cond":[{"$in":["$status",["PRESENT","WFH","ON_DUTY"]]}, {"$cond":["$half_day",0.5,1]},0]}},"late_count":{"$sum":{"$cond":[{"$gt":["$late_minutes",0]},1,0]}}}},
        ]
        map_counts={d["_id"]: d for d in db.attendance_logs.aggregate(simple)}
        cur=f_date
        while cur<=t_date:
            ds=cur.isoformat()
            if ds in map_counts:
                grouped.append({"date_str": ds, "_id_date": datetime(cur.year,cur.month,cur.day), "present_count": map_counts[ds]["present_count"], "late_count": map_counts[ds]["late_count"]})
            else:
                grouped.append({"date_str": ds, "_id_date": datetime(cur.year,cur.month,cur.day), "present_count":0, "late_count":0})
            cur+=timedelta(days=1)

    # now compute headcount per day and attendance_rate and moving_avg
    # headcount per day: count employees with joined_on <= that day
    # we can fetch all employees in dept once
    emps=list(db.employees.find({"department": department}, {"joined_on":1}))
    # build items with aggregation for rate
    items=[]
    window_rates=[]
    for g in sorted(grouped, key=lambda x: x.get("date_str")):
        d_str=g.get("date_str") or g.get("_id")
        if isinstance(d_str, dict):
            d_str=g.get("date_str")
        dt_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
        is_working = dt_obj.weekday()<5
        headcount = sum(1 for e in emps if e["joined_on"]<=d_str)
        present = g.get("present_count",0)
        late = g.get("late_count",0)
        attendance_rate=None
        if is_working and headcount>0:
            attendance_rate = round4_halfup(present/headcount)
        else:
            attendance_rate=None
        # moving avg 7d over non-null rates inside range
        window_rates.append(attendance_rate)
        if len(window_rates)>7:
            window_rates.pop(0)
        valid=[r for r in window_rates if r is not None]
        moving=None
        if valid:
            moving=round4_halfup(sum(valid)/len(valid))
        items.append({
            "date": d_str,
            "is_working_day": is_working,
            "headcount": headcount,
            "present_count": present,
            "late_count": late,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": moving
        })

    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str=Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str]=None,
    month: Optional[str]=None,
    department: Optional[str]=None,
    limit: int=Query(10, ge=1, le=50),
    date_from: Optional[str]=None,
    date_to: Optional[str]=None,
    status: Optional[str]=None,
    from_: Optional[str]=Query(None, alias="from"),
    to: Optional[str]=Query(None, alias="to"),
    page: int=Query(1, ge=1),
    page_size: int=Query(20, ge=1, le=100)
):
    db=get_db()
    if endpoint=="attendance_list":
        filt={}
        if emp_code:
            filt["emp_code"]=emp_code
        if status:
            filt["status"]=status
        if date_from or date_to:
            df={}
            if date_from:
                df["$gte"]=date_from
            if date_to:
                df["$lte"]=date_to
            filt["date"]=df
        exp=db.command("explain", {"find":"attendance_logs","filter":filt,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection":"attendance_logs","explain": exp}
    elif endpoint=="employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        s,e=month_bounds(month)
        pipeline=[{"$match":{"emp_code":emp_code,"date":{"$gte":s,"$lte":e}}},{"$group":{"_id":None,"c":{"$sum":1}}}]
        exp=db.command("explain", {"aggregate":"attendance_logs","pipeline":pipeline,"cursor":{}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection":"attendance_logs","explain": exp}
    elif endpoint=="department_summary":
        if not month:
            raise HTTPException(422, "month required")
        s,e=month_bounds(month)
        pipeline=[
            {"$match": {"joined_on": {"$lte": e}}},
            {"$lookup": {"from":"attendance_logs","let":{"ec":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$ec"]}, {"$gte":["$date",s]}, {"$lte":["$date",e]}]}}}],"as":"logs"}},
        ]
        exp=db.command("explain", {"aggregate":"employees","pipeline":pipeline,"cursor":{}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection":"employees","explain": exp}
    elif endpoint=="late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        s,e=month_bounds(month)
        pipeline=[
            {"$match":{"date":{"$gte":s,"$lte":e}}},
            {"$group":{"_id":"$emp_code","total_late":{"$sum":"$late_minutes"}}},
            {"$sort":{"total_late":-1}}
        ]
        exp=db.command("explain", {"aggregate":"attendance_logs","pipeline":pipeline,"cursor":{}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection":"attendance_logs","explain": exp}
    elif endpoint=="department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, to required")
        pipeline=[
            {"$match":{"date":{"$gte":from_,"$lte":to}}},
            {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},
            {"$unwind":"$emp"},
            {"$match":{"emp.department":department}},
            {"$group":{"_id":"$date","present_count":{"$sum":1}}}
        ]
        exp=db.command("explain", {"aggregate":"attendance_logs","pipeline":pipeline,"cursor":{}}, verbosity="executionStats")
        return {"endpoint": endpoint, "collection":"attendance_logs","explain": exp}
    else:
        raise HTTPException(404, "unknown endpoint")
