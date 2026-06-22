# main.py
import socket
import os
import threading
import time
import sqlite3
import requests
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from api.routes import router as api_router
from api.video import router as video_router
from services.camera import camera_loop

main_loop = None

def init_db():
    conn = sqlite3.connect("./parking.db")
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parking_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rfid_code TEXT,
            plate_in TEXT,
            image_in_url TEXT,
            time_in DATETIME,
            plate_out TEXT,
            image_out_url TEXT,
            time_out DATETIME
        )
    """)
    # TỐI ƯU SIÊU TỐC DATABASE: Thêm Index để tìm thẻ cực nhanh, chống full table scan
    cur.execute("CREATE INDEX IF NOT EXISTS idx_rfid_code ON parking_logs(rfid_code);")
    conn.commit()
    conn.close()

# Gọi hàm ngay trước khi tạo app
init_db()

app = FastAPI(title="Hệ thống Quản lý Bãi đỗ xe thông minh")

# --- 1. CẤU HÌNH BẢO MẬT CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 2. CẤU HÌNH THƯ MỤC TĨNH VÀ TRANG CHỦ ---
os.makedirs("static/images", exist_ok=True)
os.makedirs("static/crops", exist_ok=True)

from services.vehicle import get_image_from_cache

@app.get("/static/images/{filename}")
async def get_cached_image(filename: str):
    path = f"static/images/{filename}"
    img_bytes = get_image_from_cache(path)
    if img_bytes:
        return Response(content=img_bytes, media_type="image/jpeg")
    
    # Fallback đọc từ đĩa nếu cache chưa có hoặc đã bị trôi mất (ảnh cũ)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return Response(content=f.read(), media_type="image/jpeg")
        except Exception:
            pass
    return Response(status_code=404)

@app.get("/static/crops/{filename}")
async def get_cached_crop(filename: str):
    path = f"static/crops/{filename}"
    img_bytes = get_image_from_cache(path)
    if img_bytes:
        return Response(content=img_bytes, media_type="image/jpeg")
    
    # Fallback
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                return Response(content=f.read(), media_type="image/jpeg")
        except Exception:
            pass
    return Response(status_code=404)

app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")

# --- P2.5 FIX: Health check endpoint để monitoring trạng thái hệ thống ---
@app.get("/health")
async def health():
    """Kiểm tra sức khỏe hệ thống: camera, queue backlog, WebSocket clients."""
    from services.camera import CURRENT_FRAME_IN, CURRENT_FRAME_OUT
    from services.vehicle import image_write_queue
    from api.routes import db_write_queue
    from services.websocket import manager
    return {
        "status": "ok",
        "camera_in_active": CURRENT_FRAME_IN is not None,
        "camera_out_active": CURRENT_FRAME_OUT is not None,
        "img_queue_pending": image_write_queue.qsize(),
        "db_queue_pending": db_write_queue.qsize(),
        "ws_clients": len(manager.active_connections),
    }

# --- 3. KẾT NỐI CÁC LUỒNG API ---
app.include_router(api_router)
app.include_router(video_router)

# --- 4. LUỒNG ĐỌC THẺ RFID QUA CÁP USB (ESP32) ---
def usb_rfid_thread():
    print("[*] Khởi động luồng lắng nghe thẻ RFID qua cáp USB...")
    import serial
    import serial.tools.list_ports
    import re
    
    ser = None
    while True:
        try:
            # 1. Tự động dò tìm và kết nối cổng USB của ESP32
            if ser is None or not ser.is_open:
                esp_port = None
                for port in serial.tools.list_ports.comports():
                    # Chỉ chấp nhận cổng có chứa chuỗi 'USB' hoặc 'ACM' VÀ phải có Hardware ID của cổng USB thực tế (VID:PID)
                    if ("USB" in port.device or "ACM" in port.device) and ("VID" in port.hwid or "USB" in port.hwid):
                        # Bỏ qua cổng bluetooth nội bộ và cổng phụ của Camera USB
                        if "Bluetooth" not in port.description and "Camera" not in port.description:
                            esp_port = port.device
                            break
                
                if esp_port:
                    print(f"[*] Đang kết nối với ESP32 tại cổng: {esp_port}...")
                    
                    # QUAN TRỌNG: Tắt DTR/RTS TRƯỚC khi mở port để ESP32 không bị reset
                    ser = serial.Serial()
                    ser.port = esp_port
                    ser.baudrate = 115200
                    ser.timeout = 2.0
                    ser.dtr = False
                    ser.rts = False
                    ser.open()
                    
                    # Đợi ESP32 ổn định sau khi mở port, rồi XẢ SẠCH rác boot log trong buffer
                    time.sleep(1.0)
                    
                    # Kích hoạt thử một lệnh đọc bộ đệm để xem cổng có bị "ảo" (Zombie) không
                    _ = ser.in_waiting 
                    ser.reset_input_buffer()
                    
                    from services.serial_port import set_serial_connection
                    set_serial_connection(ser)
                    
                    print("[*] Kết nối USB thành công! (Đã xả buffer boot)")
                else:
                    time.sleep(2)
                    continue

            # 2. Đọc trực tiếp liên tục (Luồng ngầm sẽ tự ngủ chờ tín hiệu, tốn 0% CPU)
            raw_bytes = ser.readline()
            if raw_bytes:
                raw_data = raw_bytes.decode('utf-8', errors='ignore').strip()
                if raw_data:
                    # Tách tiền tố cổng VÀO/RA (nếu có)
                    gate = None
                    if raw_data.upper().startswith("IN:"):
                        gate = "in"
                        raw_data = raw_data[3:]
                    elif raw_data.upper().startswith("OUT:"):
                        gate = "out"
                        raw_data = raw_data[4:]
                    elif raw_data.upper().startswith("SYS:ERR:TIMEOUT_"):
                        gate_err = raw_data.split("_")[1].upper() # Lấy chữ IN hoặc OUT
                        err_data = {
                            "action": gate_err,
                            "warning": "BARRIER TIMEOUT! (CAR STUCK)"
                        }
                        if main_loop is not None:
                            from services.websocket import manager
                            import json
                            import asyncio
                            asyncio.run_coroutine_threadsafe(
                                manager.broadcast(json.dumps(err_data)), main_loop
                            )
                        continue

                    # LÀM SẠCH: Loại bỏ mọi ký tự ẩn, khoảng trắng, null byte có thể dính ở đầu/cuối
                    clean_str = re.sub(r'[^A-Z0-9]', '', raw_data.upper())
                    
                    # LỌC TUYỆT ĐỐI: Chuỗi còn lại bắt buộc 100% phải là ký tự Hex
                    if re.fullmatch(r'[A-F0-9]{5,14}', clean_str):
                        gate_str = gate.upper() if gate else 'TỰ ĐOÁN'
                        print(f"\n[USB-RFID] Phát hiện thẻ từ ESP32: {clean_str} (Ngõ: {gate_str})")
                        try:
                            if main_loop is not None:
                                from api.routes import handle_direct_swipe
                                import asyncio
                                asyncio.run_coroutine_threadsafe(
                                    handle_direct_swipe(clean_str, gate), main_loop
                                )
                            else:
                                print("[!] Event loop chính chưa sẵn sàng!")
                        except Exception as e:
                            print(f"[!] Không thể gọi xử lý trực tiếp: {e}")
                
        except serial.SerialException:
            print("[!] Mất kết nối USB với ESP32. Đang chờ cắm lại cáp...")
            if ser:
                try: ser.close()
                except: pass
                ser = None
                from services.serial_port import set_serial_connection
                set_serial_connection(None)
            time.sleep(2)
        except (OSError, IOError) as e:
            print(f"[!] Lỗi I/O cổng USB (zombie port): {e}. Tiến hành giải phóng và kết nối lại...")
            if ser:
                try: ser.close()
                except: pass
                ser = None
                from services.serial_port import set_serial_connection
                set_serial_connection(None)
            time.sleep(2)
        except Exception as e:
            print(f"[!] Lỗi cổng USB: {e}")
            time.sleep(1)

# --- 5. KHỞI ĐỘNG CÁC LUỒNG NGẦM ---
def get_local_ip():
    """Hàm tự động dò tìm địa chỉ IP mạng Wi-Fi của laptop"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

@app.on_event("startup")
def startup_event():
    global main_loop
    import asyncio
    main_loop = asyncio.get_running_loop()
    
    # In ra đường link trực tiếp
    local_ip = get_local_ip()
    print("\n" + "="*60)
    print("HỆ THỐNG BÃI ĐỖ XE ĐÃ SẴN SÀNG!")
    print(f"Xem trên Laptop:  http://{local_ip}:8000/")
    
    # Kích hoạt luồng RFID ngầm
    # Đổi sang dùng luồng USB
    t_rfid = threading.Thread(target=usb_rfid_thread, daemon=True)
    t_rfid.start()
    
    print("[*] Khởi động luồng Camera ngầm...")
    t = threading.Thread(target=camera_loop, daemon=True)
    t.start()