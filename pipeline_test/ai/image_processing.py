# ai/image_processing.py
import cv2
import numpy as np

def align_plate_image(img):
    """Thuật toán tìm góc nghiêng và xoay thẳng biển số"""
    try:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 50, 150, apertureSize=3)
        lines = cv2.HoughLinesP(edges, 1, np.pi / 180, 50, minLineLength=int(img.shape[1]*0.4), maxLineGap=20)
        angles = []
        
        if lines is not None:
            for line in lines:
                x1, y1, x2, y2 = line[0]
                angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
                # Chỉ lấy các đường kẻ có độ nghiêng vừa phải để làm mốc
                if -30 < angle < 30 and abs(angle) > 1.0:
                    angles.append(angle)
                    
        if len(angles) > 0:
            median_angle = np.median(angles)
            (h, w) = img.shape[:2]
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
            rotated = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
            return rotated
            
        return img 
    except Exception:
        return img