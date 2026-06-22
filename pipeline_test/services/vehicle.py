from collections import Counter, OrderedDict
import cv2
import os
import time
import numpy as np
import re
import queue
import threading
from ai.recognition import recognize_text
from config import BURST_MAX_FRAMES, BURST_DELAY_SECONDS, IMAGE_SAVE_QUALITY, CROP_SAVE_QUALITY, CAMERA_RESOLUTION, PLATE_SQUARE_RATIO_MAX, PLATE_MIN_LENGTH, CLEANUP_MAX_AGE_DAYS
from services.camera import detector, ai_lock, get_current_frame_in, get_current_frame_out, set_last_detection

# ==========================================
# CẤU HÌNH RAM CACHE & BACKGROUND WRITER
# ==========================================
# P2.3 FIX: Dùng OrderedDict để thực hiện LRU cache (đọc gần đây nhất ưu tiên giữ lại)
IMAGE_CACHE = OrderedDict()
IMAGE_CACHE_MAX_SIZE = 100
image_cache_lock = threading.Lock()

def get_image_from_cache(path: str) -> bytes:
    """Đọc ảnh nén JPEG từ RAM Cache và cập nhật LRU order"""
    with image_cache_lock:
        if path in IMAGE_CACHE:
            IMAGE_CACHE.move_to_end(path)  # Đánh dấu là recently used
            return IMAGE_CACHE[path]
        return None

def set_image_to_cache(path: str, data: bytes):
    """Ghi ảnh nén JPEG vào RAM Cache với LRU eviction policy"""
    with image_cache_lock:
        if path in IMAGE_CACHE:
            IMAGE_CACHE.move_to_end(path)
        IMAGE_CACHE[path] = data
        if len(IMAGE_CACHE) > IMAGE_CACHE_MAX_SIZE:
            # Evict phần tử ít được truy cập nhất (last=False = đầu dict = LRU)
            IMAGE_CACHE.popitem(last=False)

# P1.3 FIX: Giới hạn queue=50 để tránh OOM khi SD card bị nghến (write worker chậm)
image_write_queue = queue.Queue(maxsize=50)

def image_writer_worker():
    """Worker chạy nền ghi ảnh tuần tự xuống thẻ SD với độ ưu tiên thấp"""
    while True:
        try:
            item = image_write_queue.get()
            if item is None:
                break
            
            filepath, img_bytes = item
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            
            # Ghi trực tiếp (không dùng file tạm vì Web đã đọc từ RAM Cache)
            start_time = time.time()
            with open(filepath, "wb") as f:
                f.write(img_bytes)
            
            write_time = time.time() - start_time
            if write_time > 1.0:
                print(f"[I/O WARNING] Ghi file {filepath} tốn {write_time:.3f}s (Thẻ SD bị nghẽn)")
                
        except Exception as e:
            print(f"[I/O ERROR] Lỗi ghi ảnh ở luồng nền: {e}")
        finally:
            image_write_queue.task_done()

# Khởi động luồng ghi đĩa nền
writer_thread = threading.Thread(target=image_writer_worker, daemon=True, name="ImageWriterThread")
writer_thread.start()


def is_perfect_plate(text):
    """Kiểm tra biển số có đúng chuẩn format VN không để dừng sớm"""
    if not text or len(text) < 7: return False
    # Pattern cơ bản: 2 số + 1 chữ + (có thể thêm 1 chữ/số) + 4,5 số
    pattern = r'^[1-9][0-9][A-Z][A-Z0-9]?[0-9]{4,5}$'
    return bool(re.match(pattern, text))

def run_ai_burst(rfid_code, action_type, max_frames=BURST_MAX_FRAMES):
    """Hàm chỉ chạy AI, trả về kết quả trong RAM, không ghi file."""
    start_time = time.time()
    all_texts = []
    best_img = None
    best_crop = None
    
    try:
        for frame_idx in range(max_frames):
            frame_start = time.time()
            # 1. Bắt ảnh Real-time ngay tại thời điểm gọi, KHÔNG PHẢI CHỜ TRƯỚC
            img = get_current_frame_in() if action_type == "in" else get_current_frame_out()
            if img is None: continue
            
            # get_current_frame_in/out đã trả về bản copy an toàn (có Lock bảo vệ)
            
            # Nhờ YOLO tìm biển số (dùng Lock để không đụng chạm với luồng Camera)
            with ai_lock:
                detections = detector.detect(img)
                
            if detections is not None and len(detections) > 0:
                # Gửi tọa độ Box sang luồng Camera để vẽ lên Web
                set_last_detection(action_type == "in", detections)
                
                for i, class_id in enumerate(detections.class_id):
                    if class_id == 2: # Nếu thấy Plate
                        best_img = img # Lưu lại ảnh nét nhất
                        p_box = detections.xyxy[i]
                        px1, py1, px2, py2 = map(int, p_box)
                        h_orig, w_orig, _ = img.shape
                        p = 2
                        
                        # Cắt ảnh biển số
                        cropped_img = img[max(0, py1-p):min(h_orig, py2+p), max(0, px1-p):min(w_orig, px2+p)]
                        
                        if cropped_img.size > 0:
                            best_crop = cropped_img
                            h, w = cropped_img.shape[:2]
                            ratio = w / h if h > 0 else 0
                            
                            # Đưa vào CRNN đọc chữ
                            if 0 < ratio < PLATE_SQUARE_RATIO_MAX: # Biển vuông (2 dòng)
                                raw_text = recognize_text(cropped_img[:int(h*0.55), :]) + recognize_text(cropped_img[int(h*0.45):, :])
                                
                                # SỬA LỖI: Nếu kết quả 2-dòng quá dài (>10 ký tự), nghĩa là biển 1 dòng bị nhận nhầm là 2 dòng
                                # → OCR đọc cả 2 nửa đều ra full biển → nối lại thành rác. Đọc lại như biển 1 dòng.
                                clean_check = re.sub(r'[^A-Z0-9Đ]', '', raw_text.upper())
                                if len(clean_check) > 10:
                                    raw_text = recognize_text(cropped_img)
                            else: # Biển dài (1 dòng)
                                raw_text = recognize_text(cropped_img)
                            
                            clean_text = re.sub(r'[^A-Z0-9Đ]', '', raw_text.upper())
                            if len(clean_text) >= PLATE_MIN_LENGTH: # Chỉ lấy chữ có ý nghĩa
                                all_texts.append(clean_text)

                                # THUẬT TOÁN "EARLY EXIT": Nếu đọc ảnh đầu tiên đã chuẩn -> CHỐT LUÔN, TIẾT KIỆM 50% THỜI GIAN
                                if is_perfect_plate(clean_text):
                                    print(f"[EARLY EXIT] Biển số rõ nét đạt chuẩn, dừng phân tích ngay tại frame {frame_idx + 1}!")
                                    break # Thoát vòng lặp boxes
                                    
                # Tính FPS của frame hiện tại trong chuỗi burst và cập nhật lại lên stream
                frame_end = time.time()
                fps = 1.0 / (frame_end - frame_start) if frame_end - frame_start > 0 else 0
                set_last_detection(action_type == "in", detections, fps)

                if all_texts and is_perfect_plate(all_texts[-1]):
                    break # Thoát vòng lặp frames

            # Nếu chưa chốt được, nghỉ nhẹ cho xe nhích lên một chút rồi chụp frame tiếp theo
            if frame_idx < max_frames - 1:
                time.sleep(BURST_DELAY_SECONDS)

        # ================== BẦU CHỌN (VOTING) ==================
        plate_text = f"LOI-OCR-{rfid_code[-4:]}" # Default nếu mù hẳn
        process_time = time.time() - start_time # Tính thời gian AI đã xử lý
        if len(all_texts) > 0:
            # SỬA LỖI VOTING: Ưu tiên kết quả đạt chuẩn format biển số VN có tần suất xuất hiện nhiều nhất (bình chọn thực sự!)
            perfect_texts = [t for t in all_texts if is_perfect_plate(t)]
            if perfect_texts:
                most_common_perfect, count = Counter(perfect_texts).most_common(1)[0]
                plate_text = most_common_perfect
                print(f"[BURST CHỐT ĐƠN] Biển số: {plate_text} (Bình chọn chuẩn VN: {count}/{len(perfect_texts)} phiếu) - Tốn {process_time:.3f}s")
            else:
                most_common_text, count = Counter(all_texts).most_common(1)[0]
                plate_text = most_common_text
                print(f"[BURST CHỐT ĐƠN] Biển số: {plate_text} (Đồng thuận: {count}/{len(all_texts)} phiếu) - Tốn {process_time:.3f}s")
        else:
            print(f"[BURST THẤT BẠI] Không đọc được biển số nào trong loạt ảnh! - Tốn {process_time:.3f}s")
    finally:
        pass
        
    return plate_text, best_img, best_crop

def save_burst_results(rfid_code, action_type, best_img, best_crop, ts_str=None):
    """Nén ảnh trong RAM, lưu vào RAM Cache phục vụ Web ngay lập tức, và đẩy vào Queue ghi đĩa tuần tự."""
    start_io_time = time.time()
    
    # P1.1 FIX: Thêm timestamp vào tên file ảnh full để tránh ghi đè liên tục lên cùng một sector SD
    if ts_str is None:
        ts_str = time.strftime("%Y%m%d_%H%M%S")
    base_img_path = f"static/images/{rfid_code}_{action_type}_{ts_str}.jpg"
    # Crop giữ nguyên tên (không timestamp) vì cần lookup tại thời điểm xe RA khỏi bãi
    full_crop_filename = f"static/crops/{rfid_code}_{action_type}_burst.jpg"
    
    # 1. Encode ảnh gốc toàn cảnh thành JPEG bytes trong RAM
    if best_img is not None:
        h, w = best_img.shape[:2]
        if w > 1024 or h > 768:
            scale = min(1024 / w, 768 / h)
            new_w, new_h = int(w * scale), int(h * scale)
            best_img_resized = cv2.resize(best_img, (new_w, new_h), interpolation=cv2.INTER_AREA)
        else:
            best_img_resized = best_img
        
        success, base_img_encoded = cv2.imencode('.jpg', best_img_resized, [cv2.IMWRITE_JPEG_QUALITY, IMAGE_SAVE_QUALITY])
        base_img_bytes = base_img_encoded.tobytes() if success else b""
    else:
        blank = np.zeros((768, 1024, 3), np.uint8)
        cv2.putText(blank, "NO CAMERA DATA", (350, 384), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        success, base_img_encoded = cv2.imencode('.jpg', blank, [cv2.IMWRITE_JPEG_QUALITY, IMAGE_SAVE_QUALITY])
        base_img_bytes = base_img_encoded.tobytes() if success else b""

    # 2. Encode ảnh cắt biển số thành JPEG bytes trong RAM
    if best_crop is not None:
        success, crop_encoded = cv2.imencode('.jpg', best_crop, [cv2.IMWRITE_JPEG_QUALITY, CROP_SAVE_QUALITY])
        crop_bytes = crop_encoded.tobytes() if success else b""
    else:
        blank_crop = np.zeros((80, 200, 3), np.uint8)
        cv2.putText(blank_crop, "NO PLATE", (40, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        success, crop_encoded = cv2.imencode('.jpg', blank_crop, [cv2.IMWRITE_JPEG_QUALITY, CROP_SAVE_QUALITY])
        crop_bytes = crop_encoded.tobytes() if success else b""

    # 3. Lưu vào RAM Cache
    if base_img_bytes:
        set_image_to_cache(base_img_path, base_img_bytes)
    if crop_bytes:
        set_image_to_cache(full_crop_filename, crop_bytes)
        
    # 4. Đẩy vào Queue để ghi đĩa tuần tự ở luồng nền (non-blocking)
    # P1.3 FIX: Dùng put_nowait + try/except để không bao giờ block, drop nhẹ nếu queue đầy
    if base_img_bytes:
        try:
            image_write_queue.put_nowait((base_img_path, base_img_bytes))
        except queue.Full:
            print(f"[WARN] Image write queue đầy! Bỏ qua ghi đĩa cho ảnh gốc (vẫn có trong RAM Cache).")
    if crop_bytes:
        try:
            image_write_queue.put_nowait((full_crop_filename, crop_bytes))
        except queue.Full:
            print(f"[WARN] Image write queue đầy! Bỏ qua ghi đĩa cho ảnh crop.")
        
    io_time = time.time() - start_io_time
    print(f"[I/O] Xử lý cache ảnh hoàn tất (trong RAM), tốn {io_time:.3f}s. Đã đưa vào hàng đợi ghi đĩa.")


# ==========================================
# P1.1b FIX: CLEANUP JOB XÓA ẢNH CỢ HƠN 30 NGÀY (chạy mỗi giờ)
# Tránh thẻ SD card bị đầy sau nhiều tháng vận hành
# ==========================================
def _cleanup_old_images():
    """Xóa ảnh cũ hơn CLEANUP_MAX_AGE_DAYS ngày trong static/images và static/crops."""
    cutoff_time = time.time() - CLEANUP_MAX_AGE_DAYS * 24 * 3600
    deleted_count = 0
    for folder in ["static/images", "static/crops"]:
        if not os.path.exists(folder):
            continue
        for filename in os.listdir(folder):
            if not filename.endswith('.jpg'):
                continue
            filepath = os.path.join(folder, filename)
            try:
                if os.path.getmtime(filepath) < cutoff_time:
                    os.remove(filepath)
                    # Xóa luôn trong RAM cache nếu có
                    with image_cache_lock:
                        IMAGE_CACHE.pop(filepath, None)
                    deleted_count += 1
            except Exception as e:
                print(f"[CLEANUP] Lỗi xóa file {filepath}: {e}")
    
    if deleted_count > 0:
        print(f"[CLEANUP] Đã xóa {deleted_count} ảnh cũ hơn {CLEANUP_MAX_AGE_DAYS} ngày.")
    
    # Lên lịch lần tiếp theo sau 1 giờ
    threading.Timer(3600, _cleanup_old_images).start()

# Khởi động cleanup job (lần đầu chạy sau 1 giờ, sau đó lặp mỗi giờ)
threading.Timer(3600, _cleanup_old_images).start()
