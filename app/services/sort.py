import numpy as np

class ObjectTracker:
    def __init__(self, x_tolerance=40):
        self.tracked_objects = {}  # {object_id: {'x': ..., 'y': ..., 'frame': ..., 'miss_count': ...}}
        self.next_id = 1
        self.x_tolerance = x_tolerance
        self.matched_ids = set()

    def update(self, detections, frame_idx, frame_size):
        """
        Update the tracker with new detections.
        :param detections: List of tuples [(x, y, x2, y2), ...]
        :param frame_idx: Current frame index
        :param frame_size: Size of the frame as (height, width)
        """
        

        for detection in detections:
            x, y, x2, y2, _, _ = detection
            w = x2 - x
            h = y2 - y
            cx, cy = x + w // 2, y + h // 2  # Center of the bounding box
            cx = x
            cy = y
            matched = False

            for obj_id, obj_data in list(self.tracked_objects.items()):
                if abs(cx - obj_data['x']) <= self.x_tolerance and abs(x2 - obj_data['x2']) <= self.x_tolerance and cy >= obj_data['y'] - 5:
                    # Update tracked object
                    self.tracked_objects[obj_id] = {
                        'x': cx, 'x2': x2, 'y': cy, 'bbox': detection, 'frame': frame_idx, 'miss_count': 0
                    }
                    self.matched_ids.add(obj_id)
                    matched = True
                    break

            if not matched:
                # Create a new object ID
                if cx > 10:
                    self.tracked_objects[self.next_id] = {
                        'x': cx, 'x2': x2, 'y': cy, 'bbox': detection, 'frame': frame_idx, 'miss_count': 0
                    }
                    self.next_id += 1

        # Increment the miss count for objects that weren't matched
        for obj_id in self.tracked_objects:
            if obj_id not in self.matched_ids:
                self.tracked_objects[obj_id]['miss_count'] += 1

        # Remove stale objects
        self._remove_stale_objects(frame_idx, frame_size)

    def _remove_stale_objects(self, current_frame, frame_size, max_age=9, max_misses=2):
        """
        Remove objects not updated for 'max_age' frames or exceeded max misses.
        :param current_frame: Current frame index
        :param frame_size: Size of the frame as (height, width)
        :param max_age: Maximum age of stale objects
        :param max_misses: Maximum number of consecutive misses allowed
        """
        height, width = frame_size
        self.tracked_objects = {
            obj_id: obj_data
            for obj_id, obj_data in self.tracked_objects.items()
            if current_frame - obj_data['frame'] <= max_age
            and obj_data['miss_count'] <= max_misses
            and obj_data['y'] <= height - 50
        }

    def get_tracked_objects(self):
        return self.tracked_objects
