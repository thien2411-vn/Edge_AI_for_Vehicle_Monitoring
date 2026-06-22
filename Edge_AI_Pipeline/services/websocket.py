# services/websocket.py
import asyncio
from fastapi import WebSocket, WebSocketDisconnect

class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        
    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        
    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            
    async def broadcast(self, message: str):
        """Gửi tin nhắn đến tất cả client, tự ngắt client lỗi để không làm sập broadcast"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await asyncio.wait_for(connection.send_text(message), timeout=2.0)
            except Exception:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()