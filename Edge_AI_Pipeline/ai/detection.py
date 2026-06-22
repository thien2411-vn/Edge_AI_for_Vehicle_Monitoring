import cv2
import numpy as np
from config import CONFIDENCE_THRESHOLD, NMS_IOU_THRESHOLD, NESTED_BOX_IOU_THRESHOLD


class Detections:
    """Container nhẹ thay thế supervision.Detections.
    Tối ưu cho Raspberry Pi: không kéo thêm dependency nặng, tiết kiệm RAM và thời gian import."""

    def __init__(self, xyxy=None, confidence=None, class_id=None):
        self.xyxy = xyxy if xyxy is not None else np.empty((0, 4), dtype=np.float64)
        self.confidence = confidence if confidence is not None else np.empty(0, dtype=np.float64)
        self.class_id = class_id if class_id is not None else np.empty(0, dtype=int)

    @classmethod
    def empty(cls):
        """Trả về đối tượng Detections rỗng (không có detection nào)"""
        return cls()

    def __len__(self):
        return len(self.xyxy)


# --- TÍNH NĂNG TỰ ĐỘNG NHẬN DIỆN MÔI TRƯỜNG TFLITE ---
try:
    # pyrefly: ignore [missing-import]
    import tflite_runtime.interpreter as tflite
except ImportError:
    try:
        import tensorflow.lite as tflite
    except ImportError:
        raise ImportError("[!] Không tìm thấy thư viện tflite_runtime hoặc tensorflow.lite")

class YOLODetector:
    def __init__(self, model_path):
        self.model_path = model_path

        print(f"[*] Khởi tạo TFLite Engine (YOLO) với model: {model_path}")
        self.interpreter = tflite.Interpreter(model_path=model_path, num_threads=3)
        self.interpreter.allocate_tensors()
        self.input_details = self.interpreter.get_input_details()
        self.output_details = self.interpreter.get_output_details()
        
        input_shape = self.input_details[0]['shape']
        self.input_height = input_shape[1]
        self.input_width = input_shape[2]
        
        # TỐI ƯU MEMORY: Khởi tạo mảng đệm tĩnh 1 lần duy nhất, tránh tạo rác (Garbage) Python
        self.input_buffer = np.zeros((1, self.input_height, self.input_width, 3), dtype=np.float32)

    def detect(self, img):
        height, width = img.shape[:2]
        max_dim = max(width, height)

        # --- TIỀN XỬ LÝ CHUNG ---
        # Tối ưu cho Pi: Dùng hàm padding C++ nhanh hơn tự tạo mảng Numpy trên Python
        base_padded = cv2.copyMakeBorder(img, 0, max_dim - height, 0, max_dim - width, cv2.BORDER_CONSTANT, value=(0, 0, 0))
        img_rgb = cv2.cvtColor(base_padded, cv2.COLOR_BGR2RGB)
        
        # TỐI ƯU CPU: Dùng nội suy INTER_NEAREST nhanh gấp 3 lần INTER_LINEAR trên ARM Raspberry
        img_resized = cv2.resize(img_rgb, (self.input_width, self.input_height), interpolation=cv2.INTER_NEAREST)

        # TỐI ƯU CỰC ĐẠI: Chia và ép kiểu 1-pass trực tiếp lên buffer đã được định sẵn, giảm thiểu cache miss
        np.divide(img_resized, 255.0, out=self.input_buffer[0], casting='unsafe')

        # 2. Chạy AI
        self.interpreter.set_tensor(self.input_details[0]['index'], self.input_buffer)
        self.interpreter.invoke()
        # SỬA LỖI TFLITE NATIVE: Phải .copy() để giải phóng con trỏ nhớ nội bộ
        preds = self.interpreter.get_tensor(self.output_details[0]['index']).copy()
        
        predictions = preds[0]
        
        # 3. Lật ma trận (nếu model xuất ra [8, 25200])
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.transpose()
            
        # 4. [GIẢI MÃ TỌA ĐỘ]: Phóng to tọa độ từ [0, 1] về kích thước pixel thật
        predictions[:, 0] *= self.input_width   # cx (Tâm X)
        predictions[:, 1] *= self.input_height  # cy (Tâm Y)
        predictions[:, 2] *= self.input_width   # w  (Chiều rộng)
        predictions[:, 3] *= self.input_height  # h  (Chiều cao)

        # ========================================================
        # --- HẬU XỬ LÝ ---
        # ========================================================
        factor = max_dim / float(self.input_width)
        TARGET_CLASSES = np.array([0, 1, 2])

        valid_preds = predictions[predictions[:, 4] > CONFIDENCE_THRESHOLD]

        if len(valid_preds) == 0:
            return Detections.empty()

        class_scores_matrix = valid_preds[:, 5:]
        class_ids_array = np.argmax(class_scores_matrix, axis=1)
        max_class_scores = np.max(class_scores_matrix, axis=1)
        confidences_array = valid_preds[:, 4] * max_class_scores

        mask = (confidences_array > CONFIDENCE_THRESHOLD) & np.isin(class_ids_array, TARGET_CLASSES)

        final_preds = valid_preds[mask]
        final_confs = confidences_array[mask]
        final_class_ids = class_ids_array[mask]

        if len(final_preds) == 0:
            return Detections.empty()

        cx, cy, w, h = final_preds[:, 0], final_preds[:, 1], final_preds[:, 2], final_preds[:, 3]

        left = ((cx - w / 2) * factor).astype(int)
        top = ((cy - h / 2) * factor).astype(int)
        width_box = (w * factor).astype(int)
        height_box = (h * factor).astype(int)

        boxes = np.column_stack((left, top, width_box, height_box)).tolist()
        confidences = final_confs.tolist()
        class_ids = final_class_ids.tolist()

        # --- CLASS-AWARE NMS ---
        shifted_boxes = []
        max_wh = 4096
        for i in range(len(boxes)):
            cls_id = class_ids[i]
            shifted_boxes.append([boxes[i][0] + cls_id * max_wh, boxes[i][1] + cls_id * max_wh, boxes[i][2], boxes[i][3]])

        indices = cv2.dnn.NMSBoxes(shifted_boxes, confidences, CONFIDENCE_THRESHOLD, NMS_IOU_THRESHOLD)

        if len(indices) == 0:
            return Detections.empty()

        idx = indices.flatten()
        final_boxes = np.array(boxes)[idx]
        final_confs = np.array(confidences)[idx]
        final_class_ids = np.array(class_ids)[idx]

        xyxy = final_boxes.copy()
        xyxy[:, 2] += xyxy[:, 0]
        xyxy[:, 3] += xyxy[:, 1]

        # --- BỘ LỌC XÓA BOX LỒNG NHAU ---
        final_keep = []
        for i in range(len(xyxy)):
            keep = True
            box1_area = (xyxy[i, 2] - xyxy[i, 0]) * (xyxy[i, 3] - xyxy[i, 1])
            
            for j in range(len(xyxy)):
                if i == j: continue
                
                if final_class_ids[i] == 2 or final_class_ids[j] == 2:
                    continue

                box2_area = (xyxy[j, 2] - xyxy[j, 0]) * (xyxy[j, 3] - xyxy[j, 1])
                
                if box1_area < box2_area:
                    ix1 = max(xyxy[i, 0], xyxy[j, 0])
                    iy1 = max(xyxy[i, 1], xyxy[j, 1])
                    ix2 = min(xyxy[i, 2], xyxy[j, 2])
                    iy2 = min(xyxy[i, 3], xyxy[j, 3])

                    inter_w = max(0, ix2 - ix1)
                    inter_h = max(0, iy2 - iy1)
                    
                    if inter_w > 0 and inter_h > 0:
                        inter_area = inter_w * inter_h
                        if inter_area / box1_area > NESTED_BOX_IOU_THRESHOLD:
                            keep = False
                            break
                            
            if keep:
                final_keep.append(i)

        xyxy = xyxy[final_keep]
        final_confs = final_confs[final_keep]
        final_class_ids = final_class_ids[final_keep]

        return Detections(xyxy=xyxy, confidence=final_confs, class_id=final_class_ids)