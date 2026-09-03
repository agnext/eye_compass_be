import cv2
import numpy as np
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import Response
from app.services.xai_service import XAIService
from app.services.inference_service import get_inference_service
import json

router = APIRouter()
xai_service = XAIService()

@router.post("/generate")
async def generate_heatmap(file: UploadFile = File(...), commodity: str = Form(...), variety: str = Form("")):
    # Read image
    contents = await file.read()
    nparr = np.fromstring(contents, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if img is None:
        return Response(status_code=400, content="Invalid image format")

    # Load model
    inference_service = get_inference_service()
    inference_service.load_model(commodity, variety)
    
    # Predict
    detections, _ = inference_service.predict(img)

    # Generate Heatmap
    heatmap_img = xai_service.generate_heatmap(img, detections)

    # Convert back to JPEG to serve
    success, encoded_image = cv2.imencode('.jpg', heatmap_img)
    if not success:
        return Response(status_code=500, content="Failed to encode image")

    return Response(content=encoded_image.tobytes(), media_type="image/jpeg")
