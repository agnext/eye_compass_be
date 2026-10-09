import argparse
import time
from pathlib import Path
import os
import cv2
import torch
import torch.backends.cudnn as cudnn
from numpy import random
import numpy as np
import warnings

warnings.filterwarnings("ignore", category=UserWarning)

from models.experimental import attempt_load
from utils.datasets import LoadImages
from utils.general import check_img_size, non_max_suppression, scale_coords, set_logging
from utils.plots import plot_one_box
from utils.torch_utils import select_device, time_synchronized

global model
global output_folder

# Data Accumulators
class_counts = {}
class_total_areas = {}

def detect(save_img=False):
    source, weights, view_img, save_txt, imgsz, trace = opt.source, opt.weights, opt.view_img, opt.save_txt, opt.img_size, not opt.no_trace
    save_img = not opt.nosave and not source.endswith('.txt')
    global model

    # Directories
    save_dir = Path(opt.project)
    # ======================== NEW: Create a directory for area masks ========================
    masks_save_dir = save_dir / 'area_masks'
    masks_save_dir.mkdir(parents=True, exist_ok=True)
    # =======================================================================================

    set_logging()
    imgsz = check_img_size(imgsz, s=stride)

    if half:
        model.half()

    dataset = LoadImages(source, img_size=imgsz, stride=stride)
    names = model.module.names if hasattr(model, 'module') else model.names
    colors = [[random.randint(0, 255) for _ in range(3)] for _ in names]

    if device.type != 'cpu':
        model(torch.zeros(1, 3, imgsz, imgsz).to(device).type_as(next(model.parameters())))

    t0 = time.time()
    for path, img, im0s, vid_cap in dataset:
        img = torch.from_numpy(img).to(device)
        img = img.half() if half else img.float()
        img /= 255.0
        if img.ndimension() == 3:
            img = img.unsqueeze(0)

        t1 = time_synchronized()
        with torch.no_grad():
            pred = model(img, augment=opt.augment)[0]
        t2 = time_synchronized()

        pred = non_max_suppression(pred, opt.conf_thres, opt.iou_thres, classes=opt.classes, agnostic=opt.agnostic_nms)
        t3 = time_synchronized()

        for i, det in enumerate(pred):
            p, s, im0, frame = path, '', im0s, getattr(dataset, 'frame', 0)
            p = Path(p)
            save_path = save_dir / p.name
            im0_for_cropping = im0.copy()

            if len(det):
                det[:, :4] = scale_coords(img.shape[2:], det[:, :4], im0.shape).round()

                for c in det[:, -1].unique():
                    n = (det[:, -1] == c).sum()
                    s += f"{n} {names[int(c)]}{'s' * (n > 1)}, "

                crop_counter = 0

                for *xyxy, conf, cls in reversed(det):
                    crop_counter += 1
                    class_name = names[int(cls)]
                    confidence_score = conf.item()
                    
                    class_counts[class_name] = class_counts.get(class_name, 0) + 1

                    x1, y1, x2, y2 = map(int, xyxy)
                    cropped_tile = im0_for_cropping[y1:y2, x1:x2]

                    if cropped_tile.size > 0:
                        # ===================== REVISED AREA CALCULATION LOGIC =====================
                        hsv = cv2.cvtColor(cropped_tile, cv2.COLOR_BGR2HSV)
                        
                        lower_bound = (0, 9, 49)
                        upper_bound = (47, 126, 175)
                        mask = cv2.inRange(hsv, lower_bound, upper_bound)
                        
                        kernel = np.ones((3,3), np.uint8) # A smaller kernel can be better for preserving details
                        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=2)
                        
                        # Find ALL external contours
                        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                        
                        total_area_in_tile = 0
                        valid_contours = []
                        min_contour_area = 50  # Filter out small noise specks

                        for contour in contours:
                            area = cv2.contourArea(contour)
                            if area > min_contour_area:
                                total_area_in_tile += area
                                valid_contours.append(contour)
                        
                        # --- Create and save the visualization mask ---
                        # Create a black image with the same dimensions as the cropped tile
                        contour_visualization = np.zeros_like(cropped_tile)
                        # Draw all the valid (counted) contours in green on the black background
                        cv2.drawContours(contour_visualization, valid_contours, -1, (0, 255, 0), thickness=cv2.FILLED)
                        
                        # Generate a unique filename for the mask visualization
                        mask_filename = f"{p.stem}_crop{crop_counter}_mask.png"
                        mask_save_path = masks_save_dir / class_name / mask_filename
                        mask_save_path.parent.mkdir(parents=True, exist_ok=True) # Ensure class sub-directory exists
                        cv2.imwrite(str(mask_save_path), contour_visualization)
                        # --- End of visualization logic ---

                        class_total_areas[class_name] = class_total_areas.get(class_name, 0) + total_area_in_tile
                        # =================== END OF REVISED AREA LOGIC ==============================

                        # --- Save Cropped Tile ---
                        class_dir = save_dir / class_name
                        class_dir.mkdir(parents=True, exist_ok=True)
                        
                        tile_filename = f"{p.stem}_crop{crop_counter}_conf{confidence_score:.2f}_total_area{int(total_area_in_tile)}.jpg"
                        tile_save_path = class_dir / tile_filename
                        
                        cv2.imwrite(str(tile_save_path), cropped_tile)
                    else:
                        print(f"Warning: Skipping empty crop from {p.name}")

                    if save_img or view_img:
                        label = f'{class_name} {confidence_score:.2f}'
                        plot_one_box(xyxy, im0, label=label, color=colors[int(cls)], line_thickness=1)

            print(f'{s}Done. ({(1E3 * (t2 - t1)):.1f}ms) Inference, ({(1E3 * (t3 - t2)):.1f}ms) NMS')

            if save_img and dataset.mode == 'image':
                cv2.imwrite(str(save_path), im0)
                print(f" The full image with results is saved in: {save_path}")

    print(f'Done processing all images. ({time.time() - t0:.3f}s)')

def write_summary_report():
    """Writes the final counts and areas to a text file."""
    report_path = os.path.join(output_folder, "summary_report.txt")
    
    with open(report_path, 'w') as f:
        f.write("=========================================\n")
        f.write("      Class-wise Detection Summary\n")
        f.write("=========================================\n\n")
        
        all_classes = sorted(class_counts.keys())
        
        if not all_classes:
            f.write("No objects were detected.\n")
            return
            
        total_objects_detected = sum(class_counts.values())
        
        for class_name in all_classes:
            count = class_counts.get(class_name, 0)
            total_area = class_total_areas.get(class_name, 0)
            avg_area = (total_area / count) if count > 0 else 0
            
            f.write(f"Class: {class_name}\n")
            f.write(f"-----------------------------------------\n")
            f.write(f"  - Total Bounding Boxes : {count}\n")
            f.write(f"  - Total Segmented Area : {int(total_area)} pixels\n")
            f.write(f"  - Avg. Area per Box    : {avg_area:.2f} pixels\n\n")
            
        f.write("=========================================\n")
        f.write(f"Total Bounding Boxes Detected: {total_objects_detected}\n")
        f.write("=========================================\n")

    print(f"\nSummary report saved to: {report_path}")

if __name__ == '__main__':
    # --- Define your paths here ---
    model_path = "runs/train/synthetic_hena3/weights/best.pt"
    source_folder = '/home/aml023/Desktop/shashi/mehandi/hina new/heena25_14_26_28_.png'
    output_folder = '/home/aml023/Desktop/shashi/mehandi/output'
    
    parser = argparse.ArgumentParser()
    # ... (rest of the parser arguments are the same) ...
    parser.add_argument('--weights', nargs='+', type=str, default=model_path, help='model.pt path(s)')
    parser.add_argument('--source', type=str, default=source_folder, help='source folder')
    parser.add_argument('--img-size', type=int, default=640, help='inference size (pixels)')
    parser.add_argument('--conf-thres', type=float, default=0.4, help='object confidence threshold')
    parser.add_argument('--iou-thres', type=float, default=0.45, help='IOU threshold for NMS')
    parser.add_argument('--device', default='', help='cuda device, i.e. 0 or 0,1,2,3 or cpu')
    parser.add_argument('--view-img', action='store_true', help='display results')
    parser.add_argument('--save-txt', action='store_true', help='save results to *.txt')
    parser.add_argument('--save-conf', action='store_true', help='save confidences in --save-txt labels')
    parser.add_argument('--nosave', action='store_true', help='do not save images/videos')
    parser.add_argument('--classes', nargs='+', type=int, help='filter by class: --class 0, or --class 0 2 3')
    parser.add_argument('--agnostic-nms', action='store_true', help='class-agnostic NMS')
    parser.add_argument('--augment', action='store_true', help='augmented inference')
    parser.add_argument('--update', action='store_true', help='update all models')
    parser.add_argument('--project', default=output_folder, help='save results to project/name')
    parser.add_argument('--name', default='exp', help='save results to project/name')
    parser.add_argument('--exist-ok', action='store_true', help='existing project/name ok, do not increment')
    parser.add_argument('--no-trace', action='store_true', help='don`t trace model')
    opt = parser.parse_args()
    print(opt)
    
    output_folder = opt.project

    with torch.no_grad():
        t1 = time.time()
        
        device = select_device(opt.device)
        half = device.type != 'cpu'
        model = attempt_load(opt.weights, map_location=device)
        stride = int(model.stride.max())
        
        opt.source = source_folder
        
        detect()
        
        write_summary_report()

        print(f'Total time taken: {time.time()-t1:.3f}s')