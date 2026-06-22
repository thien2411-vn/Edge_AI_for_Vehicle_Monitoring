# api/routes.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.concurrency import run_in_threadpool
import cv2
import numpy as np
import os
import json
import re
import asyncio
import base64
import time
from datetime import datetime

from database import get_db_connection, get_reader_connection
from models import RFIDData
from config import WS_CROP_QUALITY, IMAGE_SAVE_QUALITY, CROP_SAVE_QUALITY
from services.websocket import manager
from services.vehicle import run_ai_burst, save_burst_results
from services.camera import get_current_frame_in, get_current_frame_out, set_rfid_bursting
from services.serial_port import send_command_to_esp32

router = APIRouter()

# THREAD SAFETY: Sử dụng 2 lock riêng biệt cho Lối Vào và Lối Ra.
# Tránh hoàn toàn việc xe đi vào quẹt thẻ làm khóa nghẽn cổng đi ra.
_swipe_lock_in = asyncio.Lock()
_swipe_lock_out = asyncio.Lock()

import queue
import threading

# HÀNG ĐỢI GHI SQLITE & SINGLE WRITER THREAD:
# Triệt tiêu 100% lỗi "database is locked" do nhiều luồng phụ ghi SQLite đồng thời.
db_write_queue = queue.Queue()

def sqlite_writer_worker():
    """Luồng Worker ghi SQLite duy nhất toàn hệ thống.
    - Xử lý tuần tự trên một kết nối dùng lâu dài (an toàn tuyệt đối)
    - P0.2b: Tự kết nối lại nếu DB bị lỗi (thay vì dừng im)
    - P0.2:  WAL checkpoint mỗi 5 phút bằng PASSIVE (thay vì TRUNCATE sau mỗi commit)"""
    print("[*] Khởi động luồng SQLite Writer Worker tuần tự...")
    
    conn = None
    last_checkpoint = time.time()

    def ensure_connection():
        nonlocal conn
        while conn is None:
            try:
                conn = get_db_connection()
                print("[SQLITE] Kết nối database thành công.")
            except Exception as e:
                print(f"[SQLITE] Không thể kết nối DB: {e}. Thử lại sau 5s...")
                time.sleep(5)

    ensure_connection()

    while True:
        # P0.2 FIX: Periodic WAL checkpoint mỗi 5 phút (PASSIVE = không block reader)
        if conn and time.time() - last_checkpoint > 300:
            try:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                last_checkpoint = time.time()
            except Exception as ce:
                print(f"[WAL] Lỗi checkpoint: {ce}")

        # Lấy task (timeout=1s để checkpoint có thể chạy đúng giờ)
        try:
            task = db_write_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        if task is None:
            break

        try:
            action_type, args = task
            cur = conn.cursor()
            try:
                if action_type == "insert":
                    rfid, plate, img_url, time_str = args
                    cur.execute("INSERT INTO parking_logs (rfid_code, plate_in, image_in_url, time_in) VALUES (?, ?, ?, ?)", (rfid, plate, img_url, time_str))
                    conn.commit()
                elif action_type == "update":
                    log_id, plate, img_url, time_str = args
                    cur.execute("UPDATE parking_logs SET plate_out = ?, image_out_url = ?, time_out = ? WHERE id = ?", (plate, img_url, time_str, log_id))
                    conn.commit()
                elif action_type == "update_plate_in":
                    rfid, new_plate = args
                    cur.execute("UPDATE parking_logs SET plate_in = ? WHERE id = (SELECT id FROM parking_logs WHERE rfid_code = ? ORDER BY time_in DESC LIMIT 1)", (new_plate, rfid))
                    conn.commit()
                    print(f"[DATABASE] ✅ Đã lưu thủ công Biển Vào mới: {new_plate} cho thẻ {rfid}")
                elif action_type == "update_plate_out":
                    rfid, new_plate = args
                    cur.execute("UPDATE parking_logs SET plate_out = ? WHERE id = (SELECT id FROM parking_logs WHERE rfid_code = ? ORDER BY time_in DESC LIMIT 1)", (new_plate, rfid))
                    conn.commit()
                    print(f"[DATABASE] ✅ Đã lưu thủ công Biển Ra mới: {new_plate} cho thẻ {rfid}")
            finally:
                cur.close()  # P0.1 pattern: luôn đóng cursor
        except Exception as e:
            print(f"[SQLITE WRITER ERROR] Lỗi ghi DB: {e}. Đang kết nối lại...")
            try: conn.rollback()
            except: pass
            try: conn.close()
            except: pass
            conn = None
            ensure_connection()
        finally:
            db_write_queue.task_done()

    try:
        conn.close()
    except Exception:
        pass

# Khởi chạy Worker ghi tuần tự
t_writer = threading.Thread(target=sqlite_writer_worker, daemon=True)
t_writer.start()


def get_base64_crop(crop_img):
    """Chuyển ảnh OpenCV sang Base64 gửi thẳng qua WebSocket (Chống lỗi 404 và lag I/O)"""
    if crop_img is not None:
        ret, buffer = cv2.imencode('.jpg', crop_img, [cv2.IMWRITE_JPEG_QUALITY, WS_CROP_QUALITY])
        if ret:
            return f"data:image/jpeg;base64,{base64.b64encode(buffer).decode('utf-8')}"
    return "https://placehold.co/200x80/1a1a1a/475569?text=NO+PLATE"

def parse_sqlite_time(time_val):
    if not time_val: return None
    if isinstance(time_val, str):
        try:
            return datetime.strptime(time_val, "%Y-%m-%d %H:%M:%S.%f")
        except ValueError:
            return datetime.strptime(time_val, "%Y-%m-%d %H:%M:%S")
    return time_val

# ==========================================
# HÀM HELPER ĐỂ ĐẨY SQLITE VÀO LUỒNG PHỤ (CHỐNG TREO MẠNG)
# ==========================================
def db_get_last_log(rfid):
    conn = get_reader_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, plate_in, image_in_url, time_in, plate_out FROM parking_logs WHERE rfid_code = ? ORDER BY time_in DESC LIMIT 1", (rfid,))
        return cur.fetchone()
    finally:
        cur.close()  # P0.1 FIX: Đóng cursor trong mọi trường hợp (kể cả exception)

def db_process_out_log_queue(log_id, plate_out_new, full_img_url, time_out_str):
    db_write_queue.put(("update", (log_id, plate_out_new, full_img_url, time_out_str)))

def db_process_in_log_queue(rfid, plate_in_new, full_img_url, time_in_str):
    db_write_queue.put(("insert", (rfid, plate_in_new, full_img_url, time_in_str)))

# ==========================================
# HÀM CHẠY NGẦM CHO I/O (GHI FILE & DB)
# ==========================================
async def background_save_io(rfid, action, best_img, best_crop, plate_text, time_str, log_id=None, ts_str=None):
    """Hàm chạy ngầm thực sự, chỉ ghi file và DB, không ai chờ nó cả"""
    try:
        # Ghi ảnh (I/O) - chạy trong threadpool để không block luồng I/O chính
        # P1.1c FIX: Truyền ts_str để save_burst_results ghi đúng tên file có timestamp
        await run_in_threadpool(save_burst_results, rfid, action, best_img, best_crop, ts_str)
        
        # Xây dựng URL khop với tên file thực tế đã ghi xuống đĩa
        img_url = f"/static/images/{rfid}_{action}_{ts_str}.jpg" if ts_str else f"/static/images/{rfid}_{action}.jpg"
        
        # Đẩy tác vụ ghi SQLite vào hàng đợi tuần tự (I/O)
        if action == "in":
            await run_in_threadpool(db_process_in_log_queue, rfid, plate_text, img_url, time_str)
        else: # action == "out"
            await run_in_threadpool(db_process_out_log_queue, log_id, plate_text, img_url, time_str)
        
    except Exception as e:
        print(f"[BACKGROUND I/O ERROR] Lỗi ghi file/DB: {e}")

# ==========================================
# HÀM XỬ LÝ CHẠY NGẦM (GIẢI CỨU ESP32)
# ==========================================
async def background_process_rfid(rfid: str, gate: str = None):
    start_time = time.time() # Bắt đầu bấm giờ chu trình API
    set_rfid_bursting(True) # KHÓA LUỒNG AI NGẦM TRONG SUỐT CHU TRÌNH GHI DB & Ổ CỨNG
    customer_type = "Khách Vãng Lai"
    warning_msg = None
    
    try:
        # Đẩy truy vấn DB vào ThreadPool để luồng Web không bị treo đơ
        record = await run_in_threadpool(db_get_last_log, rfid)
        response_data = {}

        # XÁC ĐỊNH HÀNH ĐỘNG (IN hay OUT)
        action = None
        if gate:
            action = gate.lower()
        else:
            if record and record[4] is None:
                action = "out"
            else:
                action = "in"

        if action == "out":
            # --- XE RA ---
            if not record or record[4] is not None:
                # Xe đang không ở trong bãi mà quẹt thẻ RA
                warning_msg = "NOT CHECKED IN!"
                # Lấy tạm record rỗng để code khỏi lỗi
                record = (None, "UNKNOWN", "https://placehold.co/1024x768/1a1a1a/475569?text=NO+IMAGE", "2000-01-01 00:00:00", None)

            log_id, plate_in, image_in_url, time_in_raw, plate_out = record
            time_in = parse_sqlite_time(time_in_raw)
            time_out = datetime.now()
            
            # 1. CHỈ CHẠY AI (Cực nhanh, ~2s)
            plate_out_new, best_img, best_crop = await run_in_threadpool(
                run_ai_burst, rfid, "out", 3
            )
            
            clean_out = re.sub(r'[^A-Z0-9Đ]', '', plate_out_new.upper())
            clean_in = re.sub(r'[^A-Z0-9Đ]', '', plate_in.upper())
            if not warning_msg and clean_out != clean_in:
                warning_msg = "PLATE MISMATCH!"

            duration = time_out - time_in
            duration_str = f"{int(duration.total_seconds()//3600):02d}:{int((duration.total_seconds()%3600)//60):02d}:{int(duration.total_seconds()%60):02d}"
            
            # 2. TẠO DỮ LIỆU GỬI LÊN WEB NGAY LẬP TỨC
            # P1.1d FIX: Dùng ts_str để URL khớp với tên file thực tế sẽ được ghi
            ts_str = time_out.strftime("%Y%m%d_%H%M%S")
            timestamp = int(time.time())  # Chỉ dùng cho cache-bust crop (crop không có timestamp)
            temp_full_url_out = f"/static/images/{rfid}_out_{ts_str}.jpg"
            temp_crop_url_out = get_base64_crop(best_crop) # Ảnh crop base64 load ngay lập tức!

            crop_in_url = "https://placehold.co/200x80/1a1a1a/475569?text=No+Crop"
            base_crop_path = f"static/crops/{rfid}_in_burst.jpg"
            from services.vehicle import get_image_from_cache
            # Chỉ nạp ảnh cắt ngõ vào nếu xe thực sự đang ở trong bãi
            if warning_msg != "NOT CHECKED IN!" and (get_image_from_cache(base_crop_path) or os.path.exists(base_crop_path)):
                crop_in_url = f"/{base_crop_path}?t={timestamp}"

            response_data = {
                "action": "OUT", "rfid": rfid, "plate_in": plate_in, "plate_out": plate_out_new,
                "img_in": image_in_url, "img_out": temp_full_url_out, "img_crop_in": crop_in_url, "img_crop_out": temp_crop_url_out,
                "time_in": time_in.strftime("%H:%M:%S"), "time_out": time_out.strftime("%H:%M:%S"), "duration": duration_str,
                "customer_type": customer_type, "warning": warning_msg
            }
            
            # 3. THẢ I/O VÀO LUỒNG NGẦM (Không bắt Web phải chờ thẻ nhớ ghi xong)
            # FIX LOGIC: Chỉ lưu lịch sử khi thẻ hợp lệ (Không có lỗi)
            if not warning_msg:
                asyncio.create_task(background_save_io(
                    rfid, "out", best_img, best_crop, plate_out_new,
                    time_out.strftime("%Y-%m-%d %H:%M:%S"), log_id=log_id, ts_str=ts_str
                ))

            # --- LỆNH ĐIỀU KHIỂN PHẦN CỨNG (ESP32) ---
            if warning_msg:
                send_command_to_esp32(f"CMD:OUT:DENY:{warning_msg}")
            else:
                send_command_to_esp32(f"CMD:OUT:OPEN:5000")

        else: # action == "in"
            # --- XE VÀO ---
            if record and record[4] is None:
                warning_msg = "ALREADY IN LOT!"

            # 1. CHỈ CHẠY AI
            plate_in_new, best_img, best_crop = await run_in_threadpool(
                run_ai_burst, rfid, "in", 3
            )
            time_in_new = datetime.now()

            # 2. TẠO DỮ LIỆU GẬI LÊN WEB
            # P1.1d FIX: Dùng ts_str để URL khớp với tên file thực tế sẽ được ghi
            ts_str = time_in_new.strftime("%Y%m%d_%H%M%S")
            temp_full_url_in = f"/static/images/{rfid}_in_{ts_str}.jpg"
            temp_crop_url_in = get_base64_crop(best_crop)
            
            response_data = {
                "action": "IN", "rfid": rfid, "plate_in": plate_in_new, "img_in": temp_full_url_in, 
                "img_crop_in": temp_crop_url_in, "time_in": time_in_new.strftime("%H:%M:%S"),
                "customer_type": customer_type, "warning": warning_msg
            }
            
            # 3. THẢ I/O VÀO LUỒNG NGẦM
            # FIX LOGIC: Chỉ tạo lịch sử mới khi thẻ hợp lệ (Không bị trùng lặp)
            if not warning_msg:
                asyncio.create_task(background_save_io(
                    rfid, "in", best_img, best_crop, plate_in_new, 
                    time_in_new.strftime("%Y-%m-%d %H:%M:%S"), ts_str=ts_str
                ))

            # --- LỆNH ĐIỀU KHIỂN PHẦN CỨNG (ESP32) ---
            if warning_msg:
                send_command_to_esp32(f"CMD:IN:DENY:{warning_msg}")
            else:
                send_command_to_esp32("CMD:IN:OPEN")
            
        # Bắn dữ liệu lên Web ngay khi AI vừa xử lý xong
        total_time = time.time() - start_time
        print(f"[DASHBOARD] Cập nhật giao diện Web thành công! (Tổng thời gian chu trình: {total_time:.3f}s)\n")
        await manager.broadcast(json.dumps(response_data))
        
    except Exception as e:
        print(f"[ERROR] Lỗi xử lý ngầm: {str(e)}")
    finally:
        set_rfid_bursting(False) # CHỈ MỞ KHÓA Yolo NGẦM KHI MỌI VIỆC ĐÃ HOÀN TẤT

# ==========================================
# HÀM BAO BỌC: Giữ lock suốt quá trình xử lý, giải phóng khi xong
# ==========================================
async def _locked_process_rfid(rfid: str, gate: str, target_lock: asyncio.Lock):
    """Wrapper giữ asyncio.Lock trong suốt chu trình xử lý thẻ"""
    try:
        await background_process_rfid(rfid, gate)
    finally:
        target_lock.release()

async def handle_direct_swipe(rfid_code: str, gate: str = None):
    """Xử lý trực tiếp thẻ từ mà không qua HTTP request (Bypass HTTP Loopback)"""
    gate = gate.lower() if gate else None
    if not gate:
        record = await run_in_threadpool(db_get_last_log, rfid_code)
        if record and record[4] is None:
            gate = "out"
        else:
            gate = "in"
            
    target_lock = _swipe_lock_in if gate == "in" else _swipe_lock_out
    
    if target_lock.locked():
        print(f"[CHỐNG SPAM] Cổng {gate.upper()} đang bận, bỏ qua thẻ nhiễu: {rfid_code}")
        return {"status": "ignored", "message": f"Cổng {gate.upper()} đang bận"}
    
    await target_lock.acquire()
    asyncio.create_task(_locked_process_rfid(rfid_code, gate, target_lock))
    return {"status": "success", "message": "Đã nhận thẻ xử lý trực tiếp"}

# ==========================================
# ENDPOINT NHẬN THẺ TỪ ESP32 (KHÔNG ĐƯỢC TREO)
# ==========================================
@router.post("/api/swipe")
async def handle_rfid_swipe(data: RFIDData):
    # Lựa chọn cổng xử lý (Nếu ESP32 gửi ngõ rõ ràng, nếu không, tự đoán)
    gate = data.gate.lower() if data.gate else None
    
    if not gate:
        # Đẩy truy vấn DB dò trạng thái vào ThreadPool
        record = await run_in_threadpool(db_get_last_log, data.rfid_code)
        if record and record[4] is None:
            gate = "out"
        else:
            gate = "in"
            
    # Lấy lock tương ứng với từng ngõ (Hai lock hoàn toàn độc lập song song)
    target_lock = _swipe_lock_in if gate == "in" else _swipe_lock_out
    
    # CHỐNG SPAM: Nếu cổng này đang bận xử lý, bỏ qua tín hiệu trùng/thẻ nhiễu
    if target_lock.locked():
        print(f"[CHỐNG SPAM] Cổng {gate.upper()} đang bận, bỏ qua thẻ nhiễu: {data.rfid_code}")
        return {"status": "ignored", "message": f"Cổng {gate.upper()} đang bận"}
    
    # Chiếm lock an toàn của cổng đó
    await target_lock.acquire()
    
    # Dùng asyncio.create_task cắt đứt hoàn toàn kết nối với ESP32 ngay lập tức
    # Lock sẽ được giải phóng trong _locked_process_rfid khi xử lý xong
    asyncio.create_task(_locked_process_rfid(data.rfid_code, gate, target_lock))
    
    return {"status": "success", "message": "Đã nhận thẻ, đang xử lý ngầm"}

from pydantic import BaseModel
class UpdatePlateRequest(BaseModel):
    rfid_code: str
    gate: str
    new_plate: str

@router.post("/api/update_plate")
async def update_plate(data: UpdatePlateRequest):
    if not data.rfid_code or data.rfid_code == "---":
        return {"status": "error", "message": "Mã thẻ không hợp lệ"}
    
    action = "update_plate_in" if data.gate.lower() == "in" else "update_plate_out"
    db_write_queue.put((action, (data.rfid_code, data.new_plate)))
    return {"status": "success", "message": f"Đã gửi yêu cầu cập nhật biển số cho cổng {data.gate}"}

@router.get("/api/logs")
def get_parking_logs():
    conn = get_reader_connection()
    cur = conn.cursor()
    
    try:
        cur.execute("""
            SELECT id, rfid_code, plate_in, time_in, time_out, plate_out
            FROM parking_logs
            ORDER BY time_in DESC LIMIT 30
        """)
        rows = cur.fetchall()
        
        logs = []
        for r in rows:
            time_in = parse_sqlite_time(r[3])
            time_out = parse_sqlite_time(r[4])
            
            fee = "-"
            if time_out is not None:  
                fee = "5,000 đ"
                
            logs.append({
                "id": r[0], "ticket": r[1], "plate": r[2], 
                "time_in": time_in.strftime("%d/%m/%Y - %H:%M:%S") if time_in else "--",
                "time_out": time_out.strftime("%d/%m/%Y - %H:%M:%S") if time_out else "--",
                "status": "Hoàn thành" if time_out else "Đang gửi", 
                "customer_type": "Khách Vãng Lai", 
                "fee": fee
            })
            
        return logs
    except Exception as e:
        print(f"[ERROR] Lỗi lấy logs: {str(e)}")
        return []
    finally:
        cur.close()

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True: 
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)