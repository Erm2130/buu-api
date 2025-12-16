import uvicorn
from fastapi import FastAPI, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import time
import json
import os
import sys
from collections import defaultdict
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime

# --- Database Imports ---
from sqlalchemy import create_engine, Column, String, Text, Integer, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- ฟังก์ชัน Print ---
def log(msg):
    now = datetime.now().strftime('%H:%M:%S')
    print(f"[{now}] {msg}", file=sys.stdout, flush=True)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------- ตั้งค่าโฟลเดอร์รูปภาพ ------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
MAPS_DIR = os.path.join(STATIC_DIR, "maps")

if not os.path.exists(MAPS_DIR):
    os.makedirs(MAPS_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ==========================================
# 💾 Database Configuration (Smart Switch)
# ==========================================
# ถ้าอยู่บน Cloud จะใช้ DATABASE_URL, ถ้าอยู่เครื่องเราจะใช้ไฟล์ local_database.db
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'local_database.db')}")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

log(f"💽 เชื่อมต่อฐานข้อมูล: {'SQLite (Local)' if 'sqlite' in DATABASE_URL else 'PostgreSQL (Cloud)'}")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- Model ตารางข้อมูล Users ---
class UserDB(Base):
    __tablename__ = "users"

    username = Column(String, primary_key=True, index=True)
    line_token = Column(String, nullable=True) # ใช้เก็บ Telegram Chat ID
    schedule_json = Column(Text, default="[]") 
    last_updated = Column(DateTime, default=datetime.now)

# สร้างตาราง
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# 📍 Logic แปลงรหัสห้อง -> ตึก & รูปภาพ
# ==========================================
# ดึง URL ของ Server อัตโนมัติ (ใช้สำหรับสร้างลิงก์รูป)
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") 
SERVER_URL = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "http://localhost:8080"

def get_room_details(room_code):
    room_code = room_code.strip()
    parts = room_code.split('-')
    prefix = parts[0].upper().strip() if len(parts) > 0 else room_code
    
    building_name = f"อาคาร {prefix}"
    if prefix == "S": building_name = "ตึก 100 ปี (สมเด็จพระเทพฯ)"
    elif prefix == "P": building_name = "อาคารวิทยาศาสตร์ (P)"
    elif prefix == "L": building_name = "อาคารเรียนรวม (L)"
    elif prefix == "ARR" or "ONLINE" in room_code.upper(): building_name = "เรียนออนไลน์จ้า"
    elif prefix == "QS2": building_name = "อาคารภูมิราชนครินทร์ (QS2)"
    elif prefix == "KB": building_name = "อาคารเคบี (KB)"
    elif prefix == "SC": building_name = "อาคารวิทยาศาสตร์ (SC)"
    elif prefix == "EN": building_name = "คณะวิศวกรรมศาสตร์"

    full_image_url = ""
    valid_extensions = [".jpg", ".png", ".jpeg"]
    for ext in valid_extensions:
        filename = f"{room_code}{ext}"
        image_path = os.path.join(MAPS_DIR, filename)
        if os.path.exists(image_path):
            full_image_url = f"{SERVER_URL}/static/maps/{filename}"
            break
    
    return building_name, full_image_url

# ------------------- Helper Functions ------------------- #
def safe_text(locator):
    try: return locator.inner_text().strip()
    except: return ""

def parse_time(time_str):
    try: return datetime.strptime(time_str, "%H:%M")
    except: return datetime.max

# ------------------- Logic ดึงข้อมูล (Scraping) ------------------- #
def extract_student_info(username, password):
    log(f"🚀 เริ่มดึงข้อมูล: {username}")
    with sync_playwright() as p:
        # บน Server ต้องใช้ headless=True เท่านั้น
        browser = p.chromium.launch(headless=True) 
        page = browser.new_page()
        try:
            page.goto("https://reg.buu.ac.th/", timeout=60000)
            try: page.wait_for_load_state("networkidle", timeout=5000)
            except: pass
            
            if page.locator("text=เข้าสู่ระบบ").count() > 0:
                page.click("text=เข้าสู่ระบบ")
                page.wait_for_selector("input[name='f_uid']", timeout=10000)
            
            page.fill("input[name='f_uid']", username)
            page.fill("input[name='f_pwd']", password)
            page.click("input[type='submit']")
            time.sleep(3)
            
            if page.locator("text=ตารางเรียน/สอบ").count() == 0:
                log("❌ Login ไม่สำเร็จ")
                return [] 
            
            log("✅ Login สำเร็จ!")
            page.click("text=ตารางเรียน/สอบ")
            time.sleep(2)
            try: page.wait_for_selector("#myTable", timeout=10000)
            except: pass
            
            log("📚 อ่านข้อมูลรายวิชา...")
            myTable_raw = {}
            rows = page.locator("//*[@id='myTable']/tbody/tr")
            for i in range(rows.count()):
                cols = rows.nth(i).locator("td")
                if cols.count() >= 2:
                    code = safe_text(cols.nth(0))
                    if code:
                        name_html = cols.nth(1).inner_html().replace("<br>", "\n").replace("<br/>", "\n")
                        name_text = page.evaluate("html => { let div = document.createElement('div'); div.innerHTML = html; return div.innerText; }", name_html)
                        lines = [x.strip() for x in name_text.split('\n') if x.strip()]
                        myTable_raw[code] = {"code": code, "name_en": lines[0], "name_th": lines[1] if len(lines)>1 else ""}
            
            log("📅 อ่านตารางเรียน...")
            mainTable_raw = []
            for i in range(3, 12):
                row = page.locator(f"//*[@id='page']/table[3]/tbody/tr/td[2]/table[3]/tbody/tr/td/table/tbody/tr[{i}]")
                if row.count() > 0:
                    cols = row.locator("td")
                    day = safe_text(cols.nth(0)) if cols.count() > 0 else ""
                    if day:
                        col_data = []
                        for j in range(1, cols.count()):
                            txt = safe_text(cols.nth(j))
                            if txt: col_data.append(txt.split())
                        mainTable_raw.append({"day": day, "columns": col_data})
            
            finalTable = []
            seen = set()
            for item in mainTable_raw:
                day = item["day"]
                for col in item["columns"]:
                    if len(col) < 1: continue
                    code = col[0]
                    room = col[2] if len(col) > 2 else "-"
                    time_val = col[3].replace("(", "").replace(")", "") if len(col) > 3 else "-"
                    key = f"{code}|{day}|{time_val}"
                    if key in seen: continue
                    seen.add(key)
                    if code in myTable_raw:
                        finalTable.append({
                            "day": day, "code": code, "name_en": myTable_raw[code]["name_en"],
                            "name_th": myTable_raw[code]["name_th"], "room": room, "time": time_val
                        })
            
            grouped = defaultdict(list)
            for x in finalTable:
                grouped[x['code']].append({"day": x['day'], "time": x['time'], "room": x['room']})
            
            result = []
            for code, schedules in grouped.items():
                result.append({
                    "code": code, "name_en": myTable_raw[code]["name_en"], "name_th": myTable_raw[code]["name_th"], "schedules": schedules
                })
            
            log(f"✅ สำเร็จ: {len(result)} วิชา")
            return result
            
        except Exception as e:
            log(f"❌ Error: {e}")
            return []
        finally:
            browser.close()

# --- Models ---
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenRequest(BaseModel):
    username: str
    line_token: str

# ==================== API ENDPOINTS ====================

@app.post("/timetable")
def api_login(req: LoginRequest, db: Session = Depends(get_db)):
    log(f"📩 Login: {req.username}")
    try:
        # 1. ดึงข้อมูล
        data = extract_student_info(req.username, req.password)
        
        # 2. ปรุงข้อมูล (ใส่รูป + ชื่อตึก)
        enriched_schedule = []
        for subject in data:
            enriched_sessions = []
            for session in subject.get("schedules", []):
                b_name, img_url = get_room_details(session["room"])
                new_session = {
                    "day": session["day"], "time": session["time"], "room": session["room"],
                    "building": b_name, "map_image": img_url
                }
                enriched_sessions.append(new_session)
            
            new_subject = subject.copy()
            new_subject["schedules"] = enriched_sessions
            enriched_schedule.append(new_subject)

        # 3. บันทึกลง Database
        user = db.query(UserDB).filter(UserDB.username == req.username).first()
        if not user:
            user = UserDB(username=req.username)
            db.add(user)
        
        user.schedule_json = json.dumps(enriched_schedule, ensure_ascii=False)
        user.last_updated = datetime.now()
        db.commit()
        
        log(f"💾 บันทึกตารางลง Database เรียบร้อย")
        return {"status": "success", "data": enriched_schedule}
        
    except Exception as e:
        log(f"❌ Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save-line-token")
def api_save_token(req: TokenRequest, db: Session = Depends(get_db)):
    log(f"📩 Save Chat ID: {req.username}")
    try:
        user = db.query(UserDB).filter(UserDB.username == req.username).first()
        if not user:
            user = UserDB(username=req.username)
            db.add(user)
        
        user.line_token = req.line_token
        db.commit()
        log(f"💾 บันทึก Chat ID เรียบร้อย")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/daily-schedule-all")
def api_n8n(db: Session = Depends(get_db)):
    log("📩 n8n เรียกข้อมูล")
    users = db.query(UserDB).filter(UserDB.line_token != None).all()
    
    # ใช้วันปัจจุบัน
    thai_days = {"Monday": "จันทร์", "Tuesday": "อังคาร", "Wednesday": "พุธ", "Thursday": "พฤหัสบดี", "Friday": "ศุกร์", "Saturday": "เสาร์", "Sunday": "อาทิตย์"}
    target_day = thai_days.get(datetime.now().strftime("%A"), "จันทร์")
    
    # Mock วันจันทร์ (ถ้าต้องการเทส ให้เอา Comment ออก)
    # target_day = "จันทร์"

    output = []
    for user in users:
        if not user.schedule_json: continue
        try: full_schedule = json.loads(user.schedule_json)
        except: continue

        classes = []
        for subj in full_schedule:
            for s in subj.get("schedules", []):
                if s.get("day") == target_day:
                    classes.append({
                        "code": subj["code"], "name": subj["name_en"],
                        "time": s["time"], "room": s["room"],
                        "building": s.get("building", ""), "map_image": s.get("map_image", "")
                    })
        
        if classes:
            classes.sort(key=lambda x: parse_time(x['time']))
            output.append({
                "username": user.username,
                "line_user_id": user.line_token,
                "day": target_day,
                "classes": classes
            })
    
    return {"count": len(output), "data": output}

if __name__ == "__main__":
    print(f"\n >>> SERVER STARTED (PORT 8080) <<<")
    uvicorn.run("ts_api:app", host="0.0.0.0", port=8080, reload=True)