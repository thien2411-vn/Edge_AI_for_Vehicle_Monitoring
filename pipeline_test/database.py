# database.py
import sqlite3
import threading

DATABASE_URL = "./parking.db"

_local = threading.local()

def get_db_connection():
    """Tạo kết nối MỚI đến SQLite (dùng cho writer thread cần connection riêng)"""
    # Thêm timeout=20 để chống lỗi văng Pi (database is locked) khi ổ cứng quá tải
    conn = sqlite3.connect(DATABASE_URL, check_same_thread=False, timeout=20)
    # NORMAL: Bảo vệ đầy đủ WAL khi mất điện, chỉ chậm hơn OFF một chút nhưng an toàn tuyệt đối
    conn.execute("PRAGMA synchronous=NORMAL;")
    # P2.1 FIX: Giảm cache từ 64MB xuống 8MB mỗi connection (tiết kiệm ~112MB RAM trên Pi 2GB)
    conn.execute("PRAGMA cache_size=-8000;")
    
    # Ép SQLite xả toàn bộ dữ liệu từ file tạm (.wal) vào file gốc (.db) để máy Windows có thể đọc được qua mạng Samba
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except:
        pass
        
    return conn

def get_reader_connection():
    """Trả về kết nối thread-local dùng lâu dài cho read-only queries.
    Giữ cache SQLite sống xuyên suốt, tránh tạo/hủy connection mỗi lần truy vấn."""
    if not hasattr(_local, 'conn') or _local.conn is None:
        _local.conn = sqlite3.connect(DATABASE_URL, check_same_thread=False, timeout=20)
        _local.conn.execute("PRAGMA synchronous=NORMAL;")
        # P2.1 FIX: Giảm cache từ 64MB xuống 8MB (tiết kiệm RAM, WAL mode đã xử lý caching tốt)
        _local.conn.execute("PRAGMA cache_size=-8000;")
    return _local.conn