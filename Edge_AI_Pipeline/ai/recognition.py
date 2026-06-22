# ai/recognition.py
import cv2
import numpy as np
import threading
from config import CRNN_MODEL_PATH, ID2CHAR

# --- KIỂM TRA THƯ VIỆN TFLITE ---
try:
    # pyrefly: ignore [missing-import]
    import tflite_runtime.interpreter as tflite
except ImportError:
    try:
        import tensorflow.lite as tflite
    except ImportError:
        raise ImportError("[!] Không tìm thấy thư viện tflite_runtime hoặc tensorflow.lite")

class CRNNRecognizer:
    def __init__(self, model_path):
        self.lock = threading.Lock() # Thêm Lock để chống đụng độ giữa các luồng quẹt thẻ
        
        print(f"[*] Khởi tạo TFLite Engine (CRNN) với model: {model_path}")
        self.interpreter = tflite.Interpreter(model_path=model_path, num_threads=2)
        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()
        
        # Cấp phát bộ nhớ 1 lần duy nhất cho TFLite (Shape chuẩn NHWC của CRNN)
        self.input_buffer = np.zeros((1, 48, 320, 3), dtype=np.float32)

    def decode_predictions(self, preds_numpy):
        preds_seq = preds_numpy[0] 
        preds_index = np.argmax(preds_seq, axis=1)
        char_list = []
        for i in range(len(preds_index)):
            if preds_index[i] != 0 and (not (i > 0 and preds_index[i - 1] == preds_index[i])):
                if preds_index[i] in ID2CHAR:
                    char_list.append(ID2CHAR[preds_index[i]])
        return ''.join(char_list)

    def recognize(self, img_bgr):
        """Nhận ảnh biển số đã cắt và trả về chuỗi ký tự"""
        # Khóa model lại khi đang đọc, các luồng khác đến sau phải đứng đợi
        with self.lock:
            try:
                # SỬA LỖI NHẬN DIỆN SAI HOẶC MÙ CHỮ (Dùng CUBIC thay vì NEAREST để không làm gãy nét chữ)
                img_resized = cv2.resize(img_bgr, (320, 48), interpolation=cv2.INTER_CUBIC)
                
                # TFLite xài chuẩn NHWC (Kênh màu ở cuối)
                # TỐI ƯU ZERO-COPY: Chuẩn hóa trực tiếp vào buffer tĩnh, tránh tạo mảng trung gian trên heap
                np.subtract(img_resized, 127.5, out=self.input_buffer[0], casting='unsafe')
                np.divide(self.input_buffer[0], 127.5, out=self.input_buffer[0])
                
                self.interpreter.set_tensor(self.input_details[0]['index'], self.input_buffer)
                self.interpreter.invoke()
                # SỬA LỖI TFLITE NATIVE: Phải .copy() để giải phóng con trỏ nhớ nội bộ
                preds_numpy = self.interpreter.get_tensor(self.output_details[0]['index']).copy()
                    
                return self.decode_predictions(preds_numpy)
            except Exception as e:
                print(f"[!] Lỗi OCR: {e}")
                return ""

# Khởi tạo một đối tượng toàn cục duy nhất
recognizer = CRNNRecognizer(CRNN_MODEL_PATH)

def recognize_text(img_bgr):
    return recognizer.recognize(img_bgr)