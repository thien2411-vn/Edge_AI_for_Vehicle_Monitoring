# services/camera.py
import cv2
import time
import asyncio
import threading
import numpy as np

from config import (
    YOLO_MODEL_PATH, COLOR_MAP, CLASS_MAP,
    STREAM_JPEG_QUALITY, BOX_DISPLAY_SECONDS,
    CAMERA_RESOLUTION, CAMERA_BUFFER_SIZE,
    CAMERA_IN_INDEX, CAMERA_OUT_INDEX
)
from ai.detection import YOLODetector

CURRENT_FRAME_IN, DISPLAY_FRAME_IN = None, None
CURRENT_FRAME_OUT, DISPLAY_FRAME_OUT = None, None

# Thêm 2 biến lưu sẵn ảnh JPEG để Web không phải nén lại liên tục
ENCODED_FRAME_IN, ENCODED_FRAME_OUT = None, None

ai_lock = threading.Lock()
_burst_count = 0  # Atomic counter: >0 khi có ít nhất 1 cổng đang burst capture
_burst_lock = threading.Lock()

# THREAD SAFETY: Lock bảo vệ frame variables chống torn frame khi burst capture
_frame_lock_in = threading.Lock()
_frame_lock_out = threading.Lock()

# P0.3 FIX: Lock bảo vệ các biến detection chống torn-read giữa AI thread (ghi) và camera thread (đọc)
_det_lock = threading.Lock()

# === BIẾN LƯU BOX CHO CAMERA STREAM ===
LAST_DET_IN, LAST_DET_OUT = None, None
DET_IN_TIME, DET_OUT_TIME = 0, 0
LAST_AI_FPS_IN, LAST_AI_FPS_OUT = 0.0, 0.0


def draw_detections(frame, detections):
    """Vẽ bounding box và label lên frame bằng OpenCV thuần (thay thế supervision).
    Nhẹ hơn supervision, không cần thêm dependency nặng trên Raspberry Pi."""
    if detections is None or len(detections) == 0:
        return frame
    for i in range(len(detections)):
        x1, y1, x2, y2 = map(int, detections.xyxy[i])
        cls_id = int(detections.class_id[i])
        color = COLOR_MAP.get(cls_id, (0, 255, 0))
        label = CLASS_MAP.get(cls_id, f"ID:{cls_id}")

        # Vẽ bounding box
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        # Vẽ label với nền màu (dễ đọc trên mọi background)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(frame, (x1, y1 - th - 8), (x1 + tw + 8, y1), color, -1)
        cv2.putText(frame, label, (x1 + 4, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return frame


def set_last_detection(is_in_gate, det, ai_fps=0.0):
    global LAST_DET_IN, LAST_DET_OUT, DET_IN_TIME, DET_OUT_TIME
    global LAST_AI_FPS_IN, LAST_AI_FPS_OUT
    # P0.3 FIX: Ghi atomic trong lock, tránh camera thread đọc DET_TIME mới nhưng det object cũ
    with _det_lock:
        if is_in_gate:
            LAST_DET_IN = det
            DET_IN_TIME = time.time()
            if ai_fps > 0:
                LAST_AI_FPS_IN = ai_fps
        else:
            LAST_DET_OUT = det
            DET_OUT_TIME = time.time()
            if ai_fps > 0:
                LAST_AI_FPS_OUT = ai_fps

def set_rfid_bursting(status: bool):
    """Bật/tắt ưu tiên AI bằng counter (an toàn khi 2 cổng burst đồng thời)"""
    global _burst_count
    with _burst_lock:
        _burst_count = max(0, _burst_count + (1 if status else -1))

print("[*] Đang load model YOLO ...")
detector = YOLODetector(YOLO_MODEL_PATH)

# ==========================================
# LUỒNG 1: CHỈ ĐỌC CAMERA (Mượt 30 FPS, Không chờ AI)
# ==========================================
def read_camera_thread(cap, is_in_gate):
    global CURRENT_FRAME_IN, DISPLAY_FRAME_IN, CURRENT_FRAME_OUT, DISPLAY_FRAME_OUT
    global ENCODED_FRAME_IN, ENCODED_FRAME_OUT
    
    frame_lock = _frame_lock_in if is_in_gate else _frame_lock_out
    cam_name = "Lối Vào" if is_in_gate else "Lối Ra"
    cam_index = CAMERA_IN_INDEX if is_in_gate else CAMERA_OUT_INDEX
    
    reconnect_delay = 2.0
    prev_cam_time = time.time()
    avg_cam_fps = 0.0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            # CỰC QUAN TRỌNG: Nếu Camera USB bị lỏng/rớt, tiến hành tái khởi động kết nối với Exponential Backoff
            print(f"[!] Mất tín hiệu Camera {cam_name}. Tiến hành kết nối lại sau {reconnect_delay:.1f}s...")
            cap.release()
            time.sleep(reconnect_delay)
            cap = cv2.VideoCapture(cam_index, cv2.CAP_V4L2)
            if cap.isOpened():
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_RESOLUTION[0])
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_RESOLUTION[1])
                cap.set(cv2.CAP_PROP_BUFFERSIZE, CAMERA_BUFFER_SIZE)
                reconnect_delay = 2.0
                print(f"[✓] Kết nối lại Camera {cam_name} thành công!")
            else:
                reconnect_delay = min(reconnect_delay * 2, 60.0)
            continue
            
        reconnect_delay = 2.0
        
        curr_time = time.time()
        fps = 1.0 / (curr_time - prev_cam_time) if curr_time - prev_cam_time > 0 else 0
        prev_cam_time = curr_time
        avg_cam_fps = 0.9 * avg_cam_fps + 0.1 * fps if avg_cam_fps > 0 else fps
        
        disp = frame.copy()
        
        # ==========================================
        # VẼ BOX LÊN STREAM (CHỈ HIỂN THỊ SAU KHI QUẸT THẺ)
        # ==========================================
        current_time = time.time()
        # P0.3 FIX: Đọc atomic snapshot trong lock để tránh torn-read
        ai_fps_to_draw = 0.0
        if is_in_gate:
            with _det_lock:
                det_snapshot = LAST_DET_IN
                det_time_snapshot = DET_IN_TIME
                ai_fps_snapshot = LAST_AI_FPS_IN
            if det_snapshot is not None and (current_time - det_time_snapshot < BOX_DISPLAY_SECONDS):
                disp = draw_detections(disp, det_snapshot)
                ai_fps_to_draw = ai_fps_snapshot
        else:
            with _det_lock:
                det_snapshot = LAST_DET_OUT
                det_time_snapshot = DET_OUT_TIME
                ai_fps_snapshot = LAST_AI_FPS_OUT
            if det_snapshot is not None and (current_time - det_time_snapshot < BOX_DISPLAY_SECONDS):
                disp = draw_detections(disp, det_snapshot)
                ai_fps_to_draw = ai_fps_snapshot

        # Vẽ FPS ở chính giữa phía trên màn hình (X=220) để tránh bị CSS cắt xén 2 bên viền
        cv2.putText(disp, f"Cam: {avg_cam_fps:.1f} FPS", (220, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        if ai_fps_to_draw > 0:
            cv2.putText(disp, f"AI : {ai_fps_to_draw:.1f} FPS", (220, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)

        # Nén JPEG một lần duy nhất tại luồng đọc để tiết kiệm CPU cực lớn cho web stream
        ret_enc, jpeg_buffer = cv2.imencode('.jpg', disp, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY])

        # THREAD SAFETY: Cập nhật frame trong Lock để burst capture không đọc frame nửa cũ nửa mới
        with frame_lock:
            if is_in_gate:
                CURRENT_FRAME_IN = frame
                DISPLAY_FRAME_IN = disp
                if ret_enc:
                    ENCODED_FRAME_IN = jpeg_buffer.tobytes()
            else:
                CURRENT_FRAME_OUT = frame
                DISPLAY_FRAME_OUT = disp
                if ret_enc:
                    ENCODED_FRAME_OUT = jpeg_buffer.tobytes()

        # CHẾ ĐỘ ƯU TIÊN AI TIẾT KIỆM CPU RASPBERRY PI
        # Nếu đang trong chu trình burst capture quẹt thẻ (IS_RFID_BURSTING=True),
        # làm chậm luồng camera đáng kể (ví dụ ngủ 150ms ~ 6 FPS) để nhường tối đa năng lực tính toán CPU cho TFLite AI Inference
        with _burst_lock:
            _is_bursting = _burst_count > 0
        if _is_bursting:
            time.sleep(0.150)
        else:
            # Nhường CPU một chút cho hệ điều hành đọc cổng USB (Tránh vòng lặp quá nhanh của OpenCV)
            time.sleep(0.01)

def camera_loop():
    """Khởi động 2 camera song song, monitor và tự restart nếu thread crash."""

    def open_cap_in():
        cap = cv2.VideoCapture(CAMERA_IN_INDEX, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"[!] CẢNH BÁO: Không thể mở Camera Lối Vào (Cổng {CAMERA_IN_INDEX})!")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_RESOLUTION[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_RESOLUTION[1])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, CAMERA_BUFFER_SIZE)
        return cap

    def open_cap_out():
        cap = cv2.VideoCapture(CAMERA_OUT_INDEX, cv2.CAP_V4L2)
        if not cap.isOpened():
            print(f"[!] CẢNH BÁO: Không thể mở Camera Lối Ra (Cổng {CAMERA_OUT_INDEX})!")
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_RESOLUTION[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_RESOLUTION[1])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, CAMERA_BUFFER_SIZE)
        return cap

    def start_thread(cap, is_in_gate):
        t = threading.Thread(target=read_camera_thread, args=(cap, is_in_gate), daemon=True)
        t.start()
        return t

    # ==========================================
    # TÁCH LÀN GIAO THÔNG CHO 2 CAMERA
    # ==========================================
    t_in = start_thread(open_cap_in(), True)
    t_out = start_thread(open_cap_out(), False)

    # P1.4 FIX: Monitor thread health mỗi 10s, tự restart nếu thread chết bất ngờ
    while True:
        time.sleep(10)
        if not t_in.is_alive():
            print("[WARN] Camera IN thread đã chết bất ngờ! Đang khởi động lại...")
            t_in = start_thread(open_cap_in(), True)
        if not t_out.is_alive():
            print("[WARN] Camera OUT thread đã chết bất ngờ! Đang khởi động lại...")
            t_out = start_thread(open_cap_out(), False)

# Các hàm Getter cho Burst Capture (có Lock bảo vệ chống torn frame)
def get_current_frame_in():
    with _frame_lock_in:
        return CURRENT_FRAME_IN.copy() if CURRENT_FRAME_IN is not None else None

def get_current_frame_out():
    with _frame_lock_out:
        return CURRENT_FRAME_OUT.copy() if CURRENT_FRAME_OUT is not None else None

# Các hàm Streaming cho Web
async def gen_frames_in():
    while True:
        with _frame_lock_in:
            encoded_frame = ENCODED_FRAME_IN
            
        if encoded_frame is not None:
            yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + encoded_frame + b'\r\n')
        else:
            # Tạo ảnh đen báo lỗi
            blank = np.zeros((CAMERA_RESOLUTION[1], CAMERA_RESOLUTION[0], 3), dtype=np.uint8)
            cv2.putText(blank, "NO CAM IN", (150, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            ret, buffer = cv2.imencode('.jpg', blank)
            if ret:
                yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
                
        # NGỦ 50ms (20 FPS) ĐỂ TIẾT KIỆM CPU RASPBERRY PI
        await asyncio.sleep(0.05)

async def gen_frames_out():
    while True:
        with _frame_lock_out:
            encoded_frame = ENCODED_FRAME_OUT
            
        if encoded_frame is not None:
            yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + encoded_frame + b'\r\n')
        else:
            # Tạo ảnh đen báo lỗi
            blank = np.zeros((CAMERA_RESOLUTION[1], CAMERA_RESOLUTION[0], 3), dtype=np.uint8)
            cv2.putText(blank, "NO CAM OUT", (150, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            ret, buffer = cv2.imencode('.jpg', blank)
            if ret:
                yield (b'--frame\r\n' b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
                
        # NGỦ 50ms (20 FPS) ĐỂ TIẾT KIỆM CPU RASPBERRY PI
        await asyncio.sleep(0.05)