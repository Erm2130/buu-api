import uvicorn
from fastapi import FastAPI, HTTPException
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

# ------------------- ตั้งค่าโฟลเดอร์ ------------------- #
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(BASE_DIR, "Database")
DB_FILE = os.path.join(DB_DIR, "users_db.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
MAPS_DIR = os.path.join(STATIC_DIR, "maps")

if not os.path.exists(DB_DIR): os.makedirs(DB_DIR, exist_ok=True)
if not os.path.exists(DB_FILE):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f, ensure_ascii=False, indent=4)

if not os.path.exists(MAPS_DIR):
    os.makedirs(MAPS_DIR, exist_ok=True)

# Mount Static Files
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# ==========================================
# 📍 Logic แปลงรหัสห้อง -> ตึก & รูปภาพ
# ==========================================
# ใช้ URL จริงถ้าอยู่บน Render, ถ้าไม่มีใช้ localhost
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL") 
SERVER_URL = RENDER_EXTERNAL_URL if RENDER_EXTERNAL_URL else "http://localhost:8080"

def get_room_details(room_code):
    room_code = room_code.strip()
    
    # 1. หาชื่อตึก
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

    # 2. หารูปภาพ (รองรับ jpg, png, jpeg)
    full_image_url = ""
    valid_extensions = [".jpg", ".png", ".jpeg", ".JPG", ".PNG"]
    
    for ext in valid_extensions:
        filename = f"{room_code}{ext}" # เช่น S-101.jpg
        image_path = os.path.join(MAPS_DIR, filename)
        
        if os.path.exists(image_path):
            full_image_url = f"{SERVER_URL}/static/maps/{filename}"
            break
    
    return building_name, full_image_url

# --- Database Utils ---
def load_db():
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except: return {}

def save_db(data):
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
        return True
    except Exception as e:
        log(f"❌ บันทึกไฟล์พลาด: {e}")
        return False

def update_user_data(username, schedule_data=None, line_token=None):
    db = load_db()
    if username not in db:
        db[username] = {"schedule": [], "line_token": ""}
    
    # --- ส่วนสำคัญ: เติมข้อมูลรูปภาพและตึกก่อนบันทึก ---
    if schedule_data is not None:
        enriched_schedule = []
        found_images = 0
        for subject in schedule_data:
            enriched_sessions = []
            for session in subject.get("schedules", []):
                # เรียกใช้ Logic หาตึกและรูป
                b_name, img_url = get_room_details(session["room"])
                if img_url: found_images += 1
                
                new_session = {
                    "day": session["day"],
                    "time": session["time"],
                    "room": session["room"],
                    "building": b_name,
                    "map_image": img_url
                }
                enriched_sessions.append(new_session)
            
            new_subject = subject.copy()
            new_subject["schedules"] = enriched_sessions
            enriched_schedule.append(new_subject)

        db[username]["schedule"] = enriched_schedule
        log(f"📦 อัปเดตตารางเรียน: {username} (เจอรูปภาพ {found_images} ห้อง)")
    
    if line_token is not None:
        db[username]["line_token"] = line_token
        log(f"🔑 อัปเดต Token: {username}")
        
    save_db(db)

# --- Models ---
class LoginRequest(BaseModel):
    username: str
    password: str

class TokenRequest(BaseModel):
    username: str
    line_token: str

def safe_text(locator):
    try: return locator.inner_text().strip()
    except: return ""

def parse_time(time_str):
    try: return datetime.strptime(time_str, "%H:%M")
    except: return datetime.max

# ------------------- Logic ดึงข้อมูล (Scraping) ------------------- #
# ใช้ Logic เดิมที่คุณแจ้งว่าทำงานได้
def extract_student_info(username, password):
    log(f"🚀 เริ่มดึงข้อมูล: {username}")
    with sync_playwright() as p:
        # ใช้ headless=True สำหรับ Server
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(viewport={'width': 1280, 'height': 720})
        page = context.new_page()
        
        try:
            page.goto("https://reg.buu.ac.th/", timeout=60000)
            try: page.wait_for_load_state("domcontentloaded", timeout=10000)
            except: pass
            
            if page.locator("text=เข้าสู่ระบบ").count() > 0:
                page.click("text=เข้าสู่ระบบ")
                try: page.wait_for_selector("input[name='f_uid']", timeout=10000)
                except: pass
            
            page.fill("input[name='f_uid']", username)
            page.fill("input[name='f_pwd']", password)
            page.click("input[type='submit']", force=True)
            time.sleep(3)
            
            if page.locator("text=ตารางเรียน/สอบ").count() == 0:
                log("❌ Login ไม่สำเร็จ")
                return [] 
            
            log("✅ Login สำเร็จ!")
            page.click("text=ตารางเรียน/สอบ")
            time.sleep(3)
            
            # --- ดึงข้อมูล (Logic เดิม) ---
            myTable_raw = {}
            base = "//*[@id='myTable']/tbody/tr"
            rows = page.locator(base)
            for row_idx in range(rows.count()):
                cols = rows.nth(row_idx).locator("td")
                if cols.count() < 2: continue
                course_code = safe_text(cols.nth(0))
                
                name_html = cols.nth(1).inner_html().replace("<br>", "\n").replace("<br/>", "\n")
                name_text = page.evaluate("html => { let div = document.createElement('div'); div.innerHTML = html; return div.innerText; }", name_html)
                lines = [x.strip() for x in name_text.split('\n') if x.strip()]
                
                if course_code != "":
                    myTable_raw[course_code] = {
                        "code": course_code,
                        "name_en": lines[0] if len(lines) > 0 else "",
                        "name_th": lines[1] if len(lines) > 1 else ""
                    }
            
            main_base = "//*[@id='page']/table[3]/tbody/tr/td[2]/table[3]/tbody/tr/td/table/tbody/tr"
            mainTable_raw = []
            
            for i in range(3, 12):
                row = page.locator(f"{main_base}[{i}]")
                if row.count() == 0: continue
                cols = row.locator("td")
                if cols.count() == 0: continue
                
                day_name = safe_text(cols.nth(0))
                if day_name == "": continue
                
                row_data = {"day": day_name, "columns": []}
                for col_idx in range(1, cols.count()):
                    html = cols.nth(col_idx).inner_html().replace("<br>", ",").replace("<br/>", ",")
                    text = page.evaluate("html => { let div = document.createElement('div'); div.innerHTML = html; return div.innerText; }", html)
                    parts = [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]
                    if len(parts) > 0:
                        row_data["columns"].append(parts)
                mainTable_raw.append(row_data)
            
            finalTable = []
            seen = set()
            for item in mainTable_raw:
                day = item["day"]
                for col in item["columns"]:
                    if len(col) < 1: continue
                    
                    schedule_code = col[0]
                    schedule_room = col[2] if len(col) > 2 else "-"
                    schedule_time_raw = col[3] if len(col) > 3 else "-"
                    schedule_time = schedule_time_raw.replace("(", "").replace(")", "")
                    
                    key = f"{schedule_code}|{day}|{schedule_time}|{schedule_room}"
                    if key in seen: continue
                    seen.add(key)
                    
                    if schedule_code in myTable_raw:
                        finalTable.append({
                            "day": day,
                            "code": schedule_code,
                            "name_en": myTable_raw[schedule_code]["name_en"],
                            "name_th": myTable_raw[schedule_code]["name_th"],
                            "room": schedule_room,
                            "time": schedule_time
                        })
            
            grouped = defaultdict(list)
            for x in finalTable:
                grouped[x['code']].append({"day": x['day'], "time": x['time'], "room": x['room']})
            
            result = []
            for code, schedules in grouped.items():
                result.append({
                    "code": code,
                    "name_en": myTable_raw[code]["name_en"],
                    "name_th": myTable_raw[code]["name_th"],
                    "schedules": schedules
                })
            
            log(f"✅ ดึงข้อมูลสำเร็จ: {len(result)} วิชา")
            return result
            
        except Exception as e:
            log(f"❌ Error: {e}")
            return []
        finally:
            browser.close()

# ------------------- API Endpoints ------------------- #
@app.post("/timetable")
def get_timetable(request: LoginRequest):
    log(f"📩 รับคำขอ Login: {request.username}")
    try:
        # 1. ดึงข้อมูลดิบ
        data = extract_student_info(request.username, request.password)
        
        # 2. บันทึก (ซึ่งจะเติมรูปภาพและตึกให้ในขั้นตอนนี้)
        update_user_data(request.username, schedule_data=data)
        
        # 3. โหลดข้อมูลที่เติมรูปแล้วกลับมาส่งให้หน้าเว็บ
        db = load_db()
        return {"status": "success", "data": db[request.username]["schedule"]}

    except Exception as e:
        log(f"💥 API Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/save-line-token")
def save_line_token(request: TokenRequest):
    log(f"📩 รับ Token ของ: {request.username}")
    try:
        update_user_data(request.username, line_token=request.line_token)
        return {"status": "success", "message": "Saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/daily-schedule-all")
def get_daily_schedule_for_n8n():
    db = load_db()
    
    # ใช้วันจริง
    thai_days = {"Monday": "จันทร์", "Tuesday": "อังคาร", "Wednesday": "พุธ", "Thursday": "พฤหัสบดี", "Friday": "ศุกร์", "Saturday": "เสาร์", "Sunday": "อาทิตย์"}
    target_day = thai_days.get(datetime.now().strftime("%A"), "จันทร์")
    
    # Mock วันจันทร์ (เอาออกเมื่อใช้จริง)
    # target_day = "จันทร์"
    
    output = []
    for username, info in db.items():
        line_token = info.get("line_token", "")
        if not line_token: continue
        
        classes = []
        for subj in info.get("schedule", []):
            for s in subj.get("schedules", []):
                if s.get("day") == target_day:
                    classes.append({
                        "code": subj["code"], 
                        "name_en": subj["name_en"], 
                        "name_th": subj["name_th"], # เพิ่ม name_th
                        "time": s["time"], "room": s["room"],
                        "building": s.get("building", ""), "map_image": s.get("map_image", "")
                    })
        if classes:
            classes.sort(key=lambda x: parse_time(x['time']))
            output.append({"username": username, "line_user_id": line_token, "day": target_day, "classes": classes})
    
    return {"count": len(output), "data": output}

if __name__ == "__main__":
    print(f"\n >>> SERVER STARTED (PORT 8080) <<<")
    uvicorn.run("main:app", host="0.0.0.0", port=8080)
