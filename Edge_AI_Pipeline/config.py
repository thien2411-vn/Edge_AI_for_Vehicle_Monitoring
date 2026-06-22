# config.py

# ==========================================
# 1. CẤU HÌNH CAMERA & ĐƯỜNG DẪN MODEL
# ==========================================
YOLO_MODEL_PATH = "models/best-fp32.tflite"
CRNN_MODEL_PATH = "models/rec_MobileNetV1Enhance.tflite"

# ==========================================
# 2. CẤU HÌNH NHẬN DIỆN (YOLO)
# ==========================================
CLASS_MAP = {
    0: "Car",
    1: "Motorbike",
    2: "Plate"
}

COLOR_MAP = {
    0: (255, 0, 255), # Car: Màu Tím
    1: (255, 255, 0), # Motorbike: Màu Xanh Lơ
    2: (0, 255, 0)    # Plate: Màu Xanh Lá
}

# ==========================================
# 3. CẤU HÌNH ĐỌC CHỮ (CRNN OCR)
# ==========================================
CHARS_LIST = [
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
    'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'K', 'L', 
    'M', 'N', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'X', 
    'Y', 'Z', 'Đ'
]

ID2CHAR = {0: ''} 
for idx, char in enumerate(CHARS_LIST):
    ID2CHAR[idx + 1] = char

# ==========================================
# 4. CẤU HÌNH TỐI ƯU TỔNG HỢP
# (Tập trung magic numbers, chỉnh 1 chỗ thay vì sửa 5 file)
# ==========================================

# --- Burst Capture ---
BURST_MAX_FRAMES = 3            # Số frame tối đa cho mỗi lần quẹt thẻ
BURST_DELAY_SECONDS = 0.03      # 30ms giữa các frame burst

# --- Detection ---
CONFIDENCE_THRESHOLD = 0.35     # Ngưỡng tin cậy YOLO
NMS_IOU_THRESHOLD = 0.4         # Ngưỡng IoU cho Non-Maximum Suppression
NESTED_BOX_IOU_THRESHOLD = 0.7  # Ngưỡng loại bỏ box lồng nhau

# --- Camera ---
CAMERA_IN_INDEX = 0             # Cổng camera lối vào (Mặc định: 0)
CAMERA_OUT_INDEX = 2            # Cổng camera lối ra (Mặc định: 2, có thể chỉnh lại nếu cần)
CAMERA_RESOLUTION = (640, 480)  # Độ phân giải camera
CAMERA_BUFFER_SIZE = 2          # Buffer tối thiểu (tránh ảnh cũ bị queue)
STREAM_JPEG_QUALITY = 65        # Chất lượng JPEG stream lên web
IMAGE_SAVE_QUALITY = 55         # Giảm xuống 55 để giảm 50% dung lượng ảnh, giúp thẻ SD ghi cực nhanh không gây lag
CROP_SAVE_QUALITY = 80          # Chất lượng ảnh crop biển số
WS_CROP_QUALITY = 80            # Chất lượng ảnh crop gửi qua WebSocket

# --- Display ---
BOX_DISPLAY_SECONDS = 3.0       # Hiển thị box trên stream sau quẹt thẻ (giây)

# --- AI & Business Logic ---
PLATE_SQUARE_RATIO_MAX = 1.9    # Tỷ lệ tối đa của biển số vuông (để cắt 2 dòng)
PLATE_MIN_LENGTH = 5            # Độ dài tối thiểu của chuỗi OCR để coi là biển số
CLEANUP_MAX_AGE_DAYS = 30       # Xóa ảnh cũ hơn số ngày này