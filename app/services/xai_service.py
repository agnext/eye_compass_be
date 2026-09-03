import cv2
import numpy as np
import logging
from typing import List, Union

logger = logging.getLogger(__name__)

class XAIService:
    @staticmethod
    def generate_heatmap(image: np.ndarray, detections: List[List[float]]) -> np.ndarray:
        """
        Generate XAI heatmap overlay for an input image using the provided detections.
        """
        try:
            if image is None:
                logger.warning("Image is None, cannot generate heatmap.")
                return image

            if not detections or len(detections) == 0:
                return image

            h, w = image.shape[:2]
            heatmap_mask = np.zeros((h, w), dtype=np.float32)

            for det in detections:
                if len(det) >= 6:
                    x1, y1, x2, y2, conf, cls = det[:6]
                    
                    x1, y1 = max(0, min(int(x1), w - 1)), max(0, min(int(y1), h - 1))
                    x2, y2 = max(0, min(int(x2), w - 1)), max(0, min(int(y2), h - 1))
                    
                    if x2 <= x1 or y2 <= y1:
                        continue
                    
                    box_width = x2 - x1
                    box_height = y2 - y1
                    
                    center_x, center_y = box_width // 2, box_height // 2
                    y_coords, x_coords = np.ogrid[:box_height, :box_width]
                    
                    dist_from_center = np.sqrt((x_coords - center_x)**2 + (y_coords - center_y)**2)
                    max_dist = np.sqrt(center_x**2 + center_y**2)
                    
                    normalized_dist = dist_from_center / max_dist if max_dist > 0 else np.zeros_like(dist_from_center)
                    box_mask = np.exp(-2 * normalized_dist**2) * float(conf)
                    
                    heatmap_mask[y1:y2, x1:x2] = np.maximum(heatmap_mask[y1:y2, x1:x2], box_mask)
            
            if heatmap_mask.max() > 0:
                heatmap_mask = heatmap_mask / heatmap_mask.max()
            else:
                return image
            
            heatmap_mask = 1.0 - heatmap_mask
            heatmap = cv2.applyColorMap(np.uint8(255 * heatmap_mask), cv2.COLORMAP_JET)
            overlay = cv2.addWeighted(image, 1.0, heatmap, 0.6, 0)
            
            # Draw bounding boxes
            for det in detections:
                if len(det) >= 6:
                    x1, y1, x2, y2, conf, cls = det[:6]
                    x1, y1 = max(0, min(int(x1), w - 1)), max(0, min(int(y1), h - 1))
                    x2, y2 = max(0, min(int(x2), w - 1)), max(0, min(int(y2), h - 1))
                    
                    if x2 <= x1 or y2 <= y1:
                        continue
                    
                    cv2.rectangle(overlay, (x1, y1), (x2, y2), (0, 0, 255), 2)
                    conf_text = f"{float(conf):.2f}"
                    text_size = cv2.getTextSize(conf_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)[0]
                    text_y = max(y1 - 5, text_size[1])
                    
                    cv2.rectangle(overlay, (x1, text_y - text_size[1] - 2), (x1 + text_size[0] + 2, text_y + 2), (0, 0, 255), -1)
                    cv2.putText(overlay, conf_text, (x1 + 1, text_y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            
            return overlay
        except Exception as e:
            logger.error(f"Error generating XAI heatmap: {e}")
            return image
