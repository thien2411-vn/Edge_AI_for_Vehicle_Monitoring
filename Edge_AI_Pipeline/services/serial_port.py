# services/serial_port.py
import serial
import unicodedata

# Đối tượng lưu trữ kết nối Serial với ESP32
active_serial = None

def set_serial_connection(ser: serial.Serial):
    """Lưu trữ kết nối Serial khi đã kết nối thành công"""
    global active_serial
    active_serial = ser

def remove_vietnamese_accents(s):
    """Xóa dấu tiếng Việt vì màn hình LCD 16x2 không hỗ trợ Unicode"""
    s = unicodedata.normalize('NFKD', s).encode('ASCII', 'ignore').decode('utf-8')
    s = s.replace('Đ', 'D').replace('đ', 'd')
    return s

def send_command_to_esp32(cmd: str):
    """Gửi lệnh xuống ESP32 (Ví dụ: CMD:IN:OPEN)"""
    global active_serial
    
    # Ép về không dấu trước khi gửi xuống mạch
    clean_cmd = remove_vietnamese_accents(cmd)
    
    if active_serial and active_serial.is_open:
        try:
            # ESP32 dùng Serial.readStringUntil('\n') nên bắt buộc có \n
            command = f"{clean_cmd}\n"
            active_serial.write(command.encode('utf-8'))
            print(f"[SERIAL] Đã gửi lệnh xuống ESP32: {clean_cmd}")
        except Exception as e:
            print(f"[SERIAL] Lỗi gửi lệnh: {e}")
    else:
        print(f"[SERIAL] KHÔNG THỂ GỬI LỆNH: Mất kết nối USB ({clean_cmd})")
