# api/video.py
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from services.camera import gen_frames_in, gen_frames_out

router = APIRouter()

@router.get("/video_feed_in")
async def video_feed_in():
    return StreamingResponse(gen_frames_in(), media_type="multipart/x-mixed-replace; boundary=frame")

@router.get("/video_feed_out")
async def video_feed_out():
    return StreamingResponse(gen_frames_out(), media_type="multipart/x-mixed-replace; boundary=frame")