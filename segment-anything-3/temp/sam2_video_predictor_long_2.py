import argparse
import json
import logging
import os
import re
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import GPUtil
import cv2
import numpy as np
import pygetwindow as gw
import torch
from tqdm import tqdm

# form some devices we need to set False
# torch.backends.cuda.enable_flash_sdp(False)
from sam2.build_sam import build_sam2_video_predictor

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def ensure_directory(path):
    """Create directory if it doesn't exist."""
    os.makedirs(path, exist_ok=True)
    return path


def resource_path(relative_path):
    """Get absolute path to resource, works for dev and for PyInstaller."""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        base_path = sys._MEIPASS
    except AttributeError:
        # In development, use the current directory
        base_path = os.path.abspath("..")

    # Construct the full path and normalize it
    full_path = os.path.normpath(os.path.join(base_path, relative_path))
    return full_path


class FrameExtractor:
    def __init__(self, video_number, prefixFileName="file", limitedImages=None, video_path_template=None,
                 output_dir=None):
        self.video_path = None
        self.video_number = video_number
        self.prefixFileName = prefixFileName
        self.limitedImages = limitedImages
        self.video_path_template = video_path_template
        self.output_dir = output_dir
        self.valid_extensions = (".jpg", ".jpeg", ".png")

    def save_frame(self, frame, frame_count):
        """Save the individual frame as a .png file."""
        frame_filename = os.path.join(self.output_dir,
                                      f'{self.prefixFileName}{self.video_number}_{frame_count:05d}.png')
        cv2.imwrite(frame_filename, frame)

    def extract_frames_in_range(self, start_frame, end_frame, progress_bar):
        """Extract and save frames for a given range of frame numbers."""
        cap = cv2.VideoCapture(self.video_path)
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

        for frame_count in range(start_frame, end_frame):
            ret, frame = cap.read()
            if not ret:
                break
            self.save_frame(frame, frame_count)
            progress_bar.update(1)

        cap.release()

    def run(self):
        """Extract frames from the video to the output directory."""
        if not self.video_path_template or not self.output_dir:
            raise ValueError("Video path template and output directory must be specified.")

        video_path = self.video_path_template.format(self.video_number)
        if not os.path.exists(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        os.makedirs(self.output_dir, exist_ok=True)
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Failed to open video: {video_path}")

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_limit = self.limitedImages if self.limitedImages is not None else total_frames

        with tqdm(total=min(frame_limit, total_frames), desc="Extracting Frames") as pbar:
            frame_count = 0
            while cap.isOpened() and frame_count < frame_limit:
                ret, frame = cap.read()
                if not ret:
                    break
                output_path = os.path.join(self.output_dir,
                                           f"{self.prefixFileName}{self.video_number}_{frame_count:05d}.jpg")
                cv2.imwrite(output_path, frame)
                frame_count += 1
                pbar.update(1)

        cap.release()
        print(f"Extracted {frame_count} frames to '{self.output_dir}'")


class VideoFrameProcessor:
    def __init__(self, video_number, batch_size=120, images_starting_count=0, images_ending_count=None,
                 prefixFileName="file", video_path_template=None, images_extract_dir=None, rendered_frames_dir=None,
                 temp_processing_dir=None, is_drawing=False, window_size=None, label_colors=None):
        if rendered_frames_dir is None:
            rendered_frames_dir = './videos/outputs'
        if window_size is None:
            window_size = [200, 200]
        if images_extract_dir is None:
            images_extract_dir = '../videos/Images'
        if label_colors is None:
            label_colors = {
                1: (0, 0, 255),
                2: (255, 0, 0),
                3: (0, 255, 0),
                4: (0, 255, 255),
                5: (255, 0, 255),
                6: (255, 255, 0),
                7: (128, 0, 128),
                8: (0, 165, 255),
                9: (255, 255, 255),
                10: (0, 0, 0),
            }
        if video_path_template is None:
            logger.error("Missing the video file paths or video")
            sys.exit(100)
        self.device = self.get_device()
        self.gpus = GPUtil.getGPUs()
        self.batch_size = batch_size
        self.video_number = video_number
        self.prefixFileName = prefixFileName
        self.video_path_template = video_path_template
        self.rendered_frames_dirs = rendered_frames_dir
        self.temp_directory = temp_processing_dir
        self.model_config = resource_path("../../sam2_configs/sam2_hiera_l.yaml")
        self.frames_directory = images_extract_dir
        self.sam2_checkpoint = resource_path("../../checkpoints/sam2_hiera_large.pt")
        self.sam2_predictor = self.build_predictor()
        extractor = FrameExtractor(self.video_number, prefixFileName=self.prefixFileName,
                                   limitedImages=images_ending_count, video_path_template=self.video_path_template,
                                   output_dir=images_extract_dir)
        extractor.run()
        self.is_drawing = is_drawing
        self.window_size = window_size
        self.label_colors = label_colors
        self.image_counter = images_starting_count
        self.current_class_label = 1
        self.current_frame = None
        self.selected_points = []
        self.selected_labels = []
        self.points_collection_list = []
        self.labels_collection_list = []
        self.color_map = self.get_color_map(9)
        self.frame_paths = self.get_frame_paths()

    def get_device(self):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        logger.info(f"Using device: {device}")
        if device.type == "cuda":
            torch.backends.cuda.matmul.allow_tf32 = True
            torch.backends.cudnn.allow_tf32 = True
        return device

    def build_predictor(self):
        predictor = build_sam2_video_predictor(
            self.model_config, self.sam2_checkpoint, device=self.device
        )
        return torch.compile(predictor, mode="reduce-overhead")

    def get_color_map(self, num_colors):
        colors = []
        for _ in range(num_colors):
            color = np.random.randint(0, 256, size=3).tolist()
            colors.append(color)
        return colors

    def save_points_and_labels(self, points_collection, labels_collection, filename=None):
        if filename is None:
            filename = f"points_labels_{self.prefixFileName}{self.video_number}.json"
        with open(filename, 'w') as f:
            json.dump({"points": points_collection, "labels": labels_collection}, f)

    def load_points_and_labels(self):
        filename = f"points_labels_{self.prefixFileName}{self.video_number}.json"
        with open(filename, 'r') as f:
            data = json.load(f)
        return data["points"], data["labels"]

    def gpu_memory_usage(self, ind=0):
        return self.gpus[ind]

    def load_user_points(self):
        self.points_collection_list, self.labels_collection_list = self.load_points_and_labels()

    def mask2colorMaskImg(self, mask):
        colors = np.array([
            [0, 0, 0],  # Black (Background)
            [0, 0, 255],  # Red
            [0, 255, 0],  # Green
            [255, 0, 0],  # Blue
            [0, 255, 255],  # Yellow
            [255, 0, 255],  # Magenta
            [255, 255, 0],  # Cyan
            [128, 0, 128],  # Purple
            [0, 165, 255],  # Orange
            [255, 255, 255],  # White
        ], dtype=np.uint8)
        max_valid_id = len(colors) - 1
        mask = np.clip(mask, 0, max_valid_id)
        logger.debug(f"np.unique(mask): {np.unique(mask)}")
        mask_image = colors[mask]
        return mask_image

    def get_frame_paths(self):
        frame_paths = []
        for p in os.listdir(self.frames_directory):
            if os.path.splitext(p)[-1].lower() in [".jpg", ".jpeg", ".png"]:
                # Extract the numeric frame index after the underscore (e.g., '00000' from 'road4_00000.jpg')
                match = re.search(r'_(\d+)\.(?:jpg|jpeg|png)$', p, re.IGNORECASE)
                if match:
                    frame_paths.append(os.path.join(self.frames_directory, p))
                else:
                    logger.warning(f"Skipping file {p} due to invalid filename format")
        # Sort by the numeric frame index
        return sorted(frame_paths,
                      key=lambda p: int(re.search(r'_(\d+)\.(?:jpg|jpeg|png)$', p, re.IGNORECASE).group(1)))

    def move_and_copy_frames(self, batch_index):
        frames_to_copy = self.frame_paths[batch_index:batch_index + self.batch_size]
        self.clear_directory(self.temp_directory)
        ensure_directory(self.temp_directory)
        for i, frame_path in enumerate(frames_to_copy):
            # Extract the frame index from the filename (e.g., '00000' from 'road4_00000.jpg')
            match = re.search(r'_(\d+)\.(?:jpg|jpeg|png)$', frame_path, re.IGNORECASE)
            if not match:
                logger.warning(f"Skipping file {frame_path} due to invalid filename format")
                continue
            frame_index = match.group(1)
            # Create a new filename with only the frame index (e.g., '00000.jpg')
            new_filename = f"{frame_index}.jpg"
            dst_path = os.path.join(self.temp_directory, new_filename)
            shutil.copy(frame_path, dst_path)
            logger.debug(f"Copied {frame_path} to {dst_path}")

    def clear_directory(self, directory):
        if os.path.exists(directory):
            for filename in os.listdir(directory):
                file_path = os.path.join(directory, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    logger.error(f"Failed to delete {file_path}. Reason: {e}")
        else:
            logger.debug(f"Directory {directory} does not exist.")

    def process_frame(self, out_frame_idx, frame_filenames, video_segments, present_count):
        frame_path = os.path.join(self.temp_directory, frame_filenames[out_frame_idx])
        frame = cv2.imread(frame_path)
        full_mask = np.zeros((frame.shape[0], frame.shape[1]), dtype=np.uint8)

        for out_obj_id in sorted(video_segments[out_frame_idx].keys(), reverse=True):
            out_mask = video_segments[out_frame_idx][out_obj_id]
            logger.debug(f"Processing object ID: {out_obj_id}")
            if out_mask.dtype == np.bool_:
                out_mask = out_mask.astype(np.uint8)
            out_mask = out_mask.squeeze()
            if out_mask.shape[:2] != (frame.shape[0], frame.shape[1]):
                out_mask_resized = cv2.resize(out_mask, (frame.shape[1], frame.shape[0]),
                                              interpolation=cv2.INTER_NEAREST_EXACT)
            else:
                out_mask_resized = out_mask
            mask_condition = (out_mask_resized > 0) & (full_mask == 0)
            full_mask[mask_condition] = out_obj_id
            logger.debug(f"np.unique(out_mask_resized): {np.unique(out_mask_resized)}, obj_id: {out_obj_id}, "
                         f"np.unique(full_mask): {np.unique(full_mask)}")

        color_mask_image = self.mask2colorMaskImg(full_mask)
        cv2.imwrite(
            os.path.join(self.rendered_frames_dirs,
                         f"{self.prefixFileName}{self.video_number}_{present_count:05d}.png"),
            color_mask_image
        )
        return present_count + 1

    def propagate_in_video_with_window(self, inference_state, window_size=100):
        video_segments = {}
        for start_idx in range(0, len(self.frame_paths), window_size):
            end_idx = min(start_idx + window_size, len(self.frame_paths))
            temp_state = self.sam2_predictor.init_state(video_path=self.frames_directory)
            self.sam2_predictor.reset_state(temp_state)
            # Copy keyframe prompts from previous window
            if start_idx > 0:
                self.sam2_predictor.add_new_points_or_box(
                    inference_state=temp_state,
                    frame_idx=0,
                    obj_id=1,
                    points=self.points_collection_list[start_idx // self.batch_size],
                    labels=self.labels_collection_list[start_idx // self.batch_size]
                )
            for out_frame_idx, out_obj_ids, out_mask_logits in self.sam2_predictor.propagate_in_video(temp_state):
                video_segments[start_idx + out_frame_idx] = {
                    out_obj_id: (out_mask_logits[i] > 0.0).cpu().numpy()
                    for i, out_obj_id in enumerate(out_obj_ids)
                }
        return video_segments

    def process_batch(self, batch_number):
        frame_filenames = sorted(
            [p for p in os.listdir(self.temp_directory) if
             os.path.splitext(p)[-1].lower() in [".jpg", ".jpeg", ".png"]],
            key=lambda p: int(re.search(r'_(\d+)\.(?:jpg|jpeg|png)$', p, re.IGNORECASE).group(1))
            if re.search(r'_(\d+)\.(?:jpg|jpeg|png)$', p, re.IGNORECASE) else float('inf')
        )
        inference_state = self.sam2_predictor.init_state(video_path=self.temp_directory)
        self.sam2_predictor.reset_state(inference_state)
        points_np = np.array(self.points_collection_list[batch_number], dtype=np.float32)
        labels_np = np.array(self.labels_collection_list[batch_number], np.int32)

        # Validate labels
        unique_labels = np.unique(np.abs(labels_np))
        max_expected_label = 10
        if any(label > max_expected_label for label in unique_labels):
            raise ValueError(
                f"Invalid labels in batch {batch_number}: {unique_labels}. Max expected: {max_expected_label}")

        ensure_directory(self.rendered_frames_dirs)
        ann_frame_idx = 0
        for ann_obj_id in set(abs(labels_np)):
            logger.debug(f"Processing object ID: {ann_obj_id}")
            labels_np1 = labels_np.copy()
            labels_np1[labels_np1 == ann_obj_id] = labels_np1[labels_np1 == ann_obj_id] // ann_obj_id
            labels_np1[labels_np1 < 0] = 0
            labels_np1 = labels_np1[abs(labels_np) == ann_obj_id]
            points_np1 = points_np[abs(labels_np) == ann_obj_id]
            _, object_ids, mask_logits = self.sam2_predictor.add_new_points_or_box(
                inference_state=inference_state,
                frame_idx=ann_frame_idx,
                clear_old_points=False,
                obj_id=int(ann_obj_id),
                points=points_np1,
                labels=labels_np1
            )
        video_segments = {}
        low_confidence_frames = []
        for out_frame_idx, out_obj_ids, out_mask_logits in self.sam2_predictor.propagate_in_video(inference_state):
            video_segments[out_frame_idx] = {
                out_obj_id: (out_mask_logits[i] > 0.0).cpu().numpy()
                for i, out_obj_id in enumerate(out_obj_ids)
            }
            mean_logits = np.mean([out_mask_logits[i].cpu().numpy() for i in range(len(out_obj_ids))])
            if mean_logits < 0.5:
                low_confidence_frames.append(out_frame_idx)
        present_count = self.image_counter
        with ThreadPoolExecutor(max_workers=os.cpu_count() - 1) as executor:
            futures = [
                executor.submit(self.process_frame, out_frame_idx, frame_filenames, video_segments, present_count + i)
                for i, out_frame_idx in enumerate(range(len(frame_filenames)))]
            for future in futures:
                present_count = max(present_count, future.result())
        self.image_counter = present_count

    def collect_user_points(self):
        self.points_collection_list = []
        self.labels_collection_list = []
        cv2.namedWindow("Zoom View", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Zoom View", self.window_size[0], self.window_size[1])
        frames_batch_paths = [self.frame_paths[i] for i in range(0, len(self.frame_paths), self.batch_size)]
        for frame_path in frames_batch_paths:
            self.current_class_label = 1
            self.current_frame = cv2.imread(frame_path)
            cv2.namedWindow("Frame", cv2.WINDOW_NORMAL)
            cv2.setMouseCallback("Frame", self.click_event)
            while True:
                display_frame = self.current_frame.copy()
                cv2.putText(display_frame, f"Class: {self.current_class_label}", (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
                cv2.imshow("Frame", display_frame)
                key = cv2.waitKey(0)
                if key == 13:  # Enter key
                    self.points_collection_list.append(self.selected_points[:])
                    self.labels_collection_list.append(self.selected_labels[:])
                    self.selected_points.clear()
                    self.selected_labels.clear()
                    break
                if key == ord('q'):
                    return
                elif key == ord('u'):  # Undo last point
                    if self.selected_points:
                        self.selected_points.pop()
                        self.selected_labels.pop()
                        self.current_frame = cv2.imread(frame_path)
                        for pt, lbl in zip(self.selected_points, self.selected_labels):
                            cv2.circle(self.current_frame, (int(pt[0]), int(pt[1])), 2, self.label_colors[lbl], -1)
                elif key == ord('1'):
                    self.change_class_label(1)
                elif key == ord('2'):
                    self.change_class_label(2)
                elif key == ord('3'):
                    self.change_class_label(3)
                elif key == ord('4'):
                    self.change_class_label(4)
                elif key == ord('5'):
                    self.change_class_label(5)
                elif key == ord('6'):
                    self.change_class_label(6)
                elif key == ord('7'):
                    self.change_class_label(7)
                elif key == ord('8'):
                    self.change_class_label(8)
                elif key == ord('9'):
                    self.change_class_label(9)
                elif key == ord('r'):
                    self.selected_points = []
                    self.selected_labels = []
                    self.current_frame = cv2.imread(frame_path)
        cv2.destroyAllWindows()
        self.save_points_and_labels(self.points_collection_list, self.labels_collection_list)

    def change_class_label(self, label):
        self.current_class_label = label

    def show_zoom_view(self, frame, x, y, zoom_factor=4, zoom_size=200):
        height, width = frame.shape[:2]
        half_zoom = zoom_size // 2
        x_start = max(x - half_zoom // zoom_factor, 0)
        x_end = min(x + half_zoom // zoom_factor, width)
        y_start = max(y - half_zoom // zoom_factor, 0)
        y_end = min(y + half_zoom // zoom_factor, height)
        zoomed_area = frame[y_start:y_end, x_start:x_end]
        zoomed_area_resized = cv2.resize(zoomed_area, (zoom_size, zoom_size), interpolation=cv2.INTER_LINEAR)
        zoom_view = np.zeros((zoom_size, zoom_size, 3), dtype=np.uint8)
        zoom_view[:zoom_size, :zoom_size] = zoomed_area_resized
        scaled_x = zoom_size // 2
        scaled_y = zoom_size // 2
        cv2.circle(zoom_view, (scaled_x, scaled_y), 5, (0, 255, 0), -1)
        return zoom_view

    def click_event(self, event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.selected_points.append([x, y])
            self.selected_labels.append(self.current_class_label)
            cv2.circle(self.current_frame, (x, y), 2, self.label_colors[self.current_class_label], -1)
        elif event == cv2.EVENT_MOUSEMOVE:
            if self.is_drawing:
                self.selected_points.append([x, y])
                self.selected_labels.append(self.current_class_label)
                cv2.circle(self.current_frame, (x, y), 2, self.label_colors[self.current_class_label], -1)
            zoom_view = self.show_zoom_view(self.current_frame, x, y)
            cv2.imshow("Zoom View", zoom_view)
            try:
                zoom_window = gw.getWindowsWithTitle("Zoom View")[0]
                zoom_window.activate()
            except IndexError:
                pass
        elif event == cv2.EVENT_LBUTTONUP:
            self.is_drawing = False
        elif event == cv2.EVENT_RBUTTONDOWN:
            self.selected_points.append([x, y])
            self.selected_labels.append(self.current_class_label * -1)
            cv2.circle(self.current_frame, (x, y), 4, (0, 0, 255), -1)
        cv2.imshow("Frame", self.current_frame)

    def collect_user_points_first_frame(self):
        self.points_collection_list = []
        self.labels_collection_list = []
        cv2.namedWindow("Zoom View", cv2.WINDOW_NORMAL)
        cv2.resizeWindow("Zoom View", self.window_size[0], self.window_size[1])
        frame_path = self.frame_paths[0]  # First frame only
        self.current_frame = cv2.imread(frame_path)
        cv2.namedWindow("Frame", cv2.WINDOW_NORMAL)
        cv2.setMouseCallback("Frame", self.click_event)
        while True:
            display_frame = self.current_frame.copy()
            cv2.putText(display_frame, f"Class: {self.current_class_label}", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.imshow("Frame", display_frame)
            key = cv2.waitKey(0)
            if key == 13:  # Enter key
                self.points_collection_list.append(self.selected_points[:])
                self.labels_collection_list.append(self.selected_labels[:])
                self.selected_points.clear()
                self.selected_labels.clear()
                break
            if key == ord('q'):
                return
            elif key == ord('u'):  # Undo last point
                if self.selected_points:
                    self.selected_points.pop()
                    self.selected_labels.pop()
                    self.current_frame = cv2.imread(frame_path)
                    for pt, lbl in zip(self.selected_points, self.selected_labels):
                        cv2.circle(self.current_frame, (int(pt[0]), int(pt[1])), 2, self.label_colors[lbl], -1)
            elif key == ord('1'):
                self.change_class_label(1)
            elif key == ord('2'):
                self.change_class_label(2)
            elif key == ord('3'):
                self.change_class_label(3)
            elif key == ord('4'):
                self.change_class_label(4)
            elif key == ord('5'):
                self.change_class_label(5)
            elif key == ord('6'):
                self.change_class_label(6)
            elif key == ord('7'):
                self.change_class_label(7)
            elif key == ord('8'):
                self.change_class_label(8)
            elif key == ord('9'):
                self.change_class_label(9)
            elif key == ord('r'):
                self.selected_points = []
                self.selected_labels = []
                self.current_frame = cv2.imread(frame_path)
        cv2.destroyAllWindows()
        self.save_points_and_labels(self.points_collection_list, self.labels_collection_list)

    def run(self):
        if os.path.exists(f"points_labels_{self.prefixFileName}{self.video_number}.json"):
            self.load_user_points()
        else:
            self.collect_user_points_first_frame()
        batch_index = 0
        while batch_index < len(self.frame_paths):
            batch_num = batch_index // self.batch_size
            if batch_num >= len(self.points_collection_list):
                self.points_collection_list.append(self.points_collection_list[-1])
                self.labels_collection_list.append(self.labels_collection_list[-1])
            logger.info(
                f"Processing batch {(batch_index + 1) // self.batch_size + 1}/{(len(self.frame_paths) // self.batch_size) + 1}")
            self.move_and_copy_frames(batch_index)
            self.process_batch(batch_num)
            batch_index += self.batch_size
            logger.info('-' * 28 + " completed " + '-' * 28)
        self.clear_directory(self.temp_directory)


class ImageOverlayProcessor:
    def __init__(self, original_folder, mask_folder, output_folder, all_consider='', image_count=0):
        self.original_folder = original_folder
        self.mask_folder = mask_folder
        self.output_folder = output_folder
        self.all_consider = all_consider
        self.image_count = image_count
        self.valid_extensions = ('.png', '.jpg', '.jpeg')
        ensure_directory(self.output_folder)
        self.original_images = self._filter_original_images()

    def _filter_original_images(self):
        all_images = sorted(
            [img for img in os.listdir(self.original_folder) if img.lower().endswith(self.valid_extensions)]
        )
        if self.all_consider:
            return all_images
        filtered_images = []
        count = 0
        for img in all_images:
            if img.split('_')[0] == self.all_consider:
                if count >= self.image_count:
                    filtered_images.append(img)
                count += 1
        return filtered_images

    def load_image_and_mask(self, image_name):
        original_image_path = os.path.join(self.original_folder, image_name)
        mask_image_name = os.path.splitext(image_name)[0] + '.png'
        mask_image_path = os.path.join(self.mask_folder, mask_image_name)
        if not os.path.exists(mask_image_path):
            return None, None
        original_image = cv2.imread(original_image_path)
        mask_image = cv2.imread(mask_image_path, cv2.IMREAD_GRAYSCALE)
        return original_image, mask_image

    def overlay_mask_on_image(self, original_image, mask_image):
        if len(mask_image.shape) == 2:
            mask_image = cv2.cvtColor(mask_image, cv2.COLOR_GRAY2BGR)
        return cv2.addWeighted(original_image, 0.5, mask_image, 0.5, 0)

    def process_image(self, img_name):
        original_image, mask_image = self.load_image_and_mask(img_name)
        if original_image is not None and mask_image is not None:
            combined_image = self.overlay_mask_on_image(original_image, mask_image)
            output_image_path = os.path.join(self.output_folder, img_name)
            cv2.imwrite(output_image_path, combined_image)

    def process_all_images(self):
        with tqdm(total=len(self.original_images), desc="Processing Images") as pbar:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(self.process_image, img_name): img_name for img_name in self.original_images}
                for future in futures:
                    future.result()
                    pbar.update(1)


class ImageCopier:
    def __init__(self, original_folder, mask_folder, overlap_images_folder, output_original_folder, output_mask_folder):
        self.original_folder = original_folder
        self.mask_folder = mask_folder
        self.overlap_images_folder = overlap_images_folder
        self.output_original_folder = output_original_folder
        self.output_mask_folder = output_mask_folder
        self.valid_extensions = ('.png', '.jpg', '.jpeg')

    def copy_image(self, src, dst):
        if os.path.exists(dst):
            base, ext = os.path.splitext(os.path.basename(src))
            counter = 1
            while os.path.exists(dst):
                new_filename = f"{base}_{counter}{ext}"
                dst = os.path.join(os.path.dirname(dst), new_filename)
                counter += 1
        shutil.copy2(src, dst)

    def _get_overlap_filenames(self):
        return {os.path.splitext(filename)[0].lower() for filename in os.listdir(self.overlap_images_folder)
                if filename.lower().endswith(self.valid_extensions)}

    def _filter_images_to_copy(self, images):
        overlap_filenames = self._get_overlap_filenames()
        return [img for img in images if os.path.splitext(os.path.basename(img))[0].lower() in overlap_filenames]

    def copy_images(self):
        ensure_directory(self.output_original_folder)
        ensure_directory(self.output_mask_folder)
        original_images = [os.path.join(self.original_folder, filename) for filename in os.listdir(self.original_folder)
                           if filename.endswith(self.valid_extensions)]
        mask_images = [os.path.join(self.mask_folder, filename) for filename in os.listdir(self.mask_folder)
                       if filename.endswith(self.valid_extensions)]
        original_images_to_copy = self._filter_images_to_copy(original_images)
        mask_images_to_copy = self._filter_images_to_copy(mask_images)
        with tqdm(total=len(original_images_to_copy), desc='Copying Original Images') as pbar:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(self.copy_image, img,
                                           os.path.join(self.output_original_folder, os.path.basename(img))): img
                           for img in original_images_to_copy}
                for future in futures:
                    future.result()
                    pbar.update(1)
        with tqdm(total=len(mask_images_to_copy), desc='Copying Mask Images') as pbar:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = {executor.submit(self.copy_image, img,
                                           os.path.join(self.output_mask_folder, os.path.basename(img))): img
                           for img in mask_images_to_copy}
                for future in futures:
                    future.result()
                    pbar.update(1)


class VideoCreator:
    def __init__(self, image_folders, video_names, fps=30):
        self.image_folders = image_folders
        self.video_names = video_names
        self.fps = fps
        self.valid_extensions = ('.png', '.jpg', '.jpeg')
        self.lock = threading.Lock()
        self.total_images = sum(len([img for img in os.listdir(folder) if img.endswith(self.valid_extensions)])
                                for folder in image_folders)
        self.processed_images = 0

    def create_video(self, image_folder, video_name, progress_bar):
        images = sorted([img for img in os.listdir(image_folder) if img.endswith(self.valid_extensions)])
        if not images:
            logger.warning(f"No images found in {image_folder}.")
            return
        first_image = os.path.join(image_folder, images[0])
        frame = cv2.imread(first_image)
        height, width, layers = frame.shape
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        video = cv2.VideoWriter(video_name, fourcc, self.fps, (width, height))
        try:
            for image in images:
                img_path = os.path.join(image_folder, image)
                img = cv2.imread(img_path)
                video.write(img)
                with self.lock:
                    self.processed_images += 1
                    progress_bar.update(1)
        finally:
            video.release()
        cap = cv2.VideoCapture(video_name)
        if not cap.isOpened():
            logger.error(f"Failed to create valid video: {video_name}")
        else:
            logger.info(f"Video saved as {video_name}")
        cap.release()

    def run(self):
        with tqdm(total=self.total_images, desc="Creating Videos", unit="frame") as pbar:
            threads = []
            for folder, name in zip(self.image_folders, self.video_names):
                thread = threading.Thread(target=self.create_video, args=(folder, name, pbar))
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()


def run_pipeline(video_number, video_path_template, images_extract_dir, rendered_dirs, overlap_dir, verified_img_dir,
                 verified_mask_dir, prefix, batch_size, fps, final_video_path, temp_processing_dir, delete):
    """Run the entire pipeline for a single video number."""
    logger.info(f"Processing video {video_number}")

    processor = VideoFrameProcessor(
        video_number=video_number,
        prefixFileName=prefix,
        batch_size=batch_size,
        video_path_template=video_path_template,
        images_extract_dir=images_extract_dir,
        rendered_frames_dir=rendered_dirs,
        temp_processing_dir=temp_processing_dir
    )
    processor.run()

    overlay_processor = ImageOverlayProcessor(
        original_folder=images_extract_dir,
        mask_folder=rendered_dirs,
        output_folder=overlap_dir,
        all_consider=prefix,
        image_count=0
    )
    overlay_processor.process_all_images()
    while delete != 'yes':
        user_input = input(
            "Have you verified all the overlay masks on original images? Enter 'yes' to proceed or 'no' to exit: ").lower()
        if user_input == 'yes':
            break
        elif user_input == 'no':
            logger.info("Pipeline terminated: Verification not completed")
            sys.exit(0)

    logger.info(f"Copying verified images and masks by default input {delete == 'yes'}")

    copier = ImageCopier(
        original_folder=images_extract_dir,
        mask_folder=rendered_dirs,
        overlap_images_folder=overlap_dir,
        output_original_folder=verified_img_dir,
        output_mask_folder=verified_mask_dir
    )
    copier.copy_images()

    ensure_directory(final_video_path)
    video_names = [f"{final_video_path}/OrgVideo{video_number}.mp4", f"{final_video_path}/MaskVideo{video_number}.mp4",
                   f"{final_video_path}/OverlappedVideo{video_number}.mp4"]
    video_creator = VideoCreator(
        image_folders=[verified_img_dir, verified_mask_dir, overlap_dir],
        video_names=video_names,
        fps=fps
    )
    video_creator.run()


def main():
    parser = argparse.ArgumentParser(description="Automated video processing pipeline.")
    parser.add_argument('--video_start', type=int, default=4, help='Starting video number (inclusive)')
    parser.add_argument('--video_end', type=int, default=1, help='Ending video number (exclusive)')
    parser.add_argument('--prefix', type=str, default='Img', help='Prefix for output filenames')
    parser.add_argument('--batch_size', type=int, default=120, help='Batch size for processing frames')
    parser.add_argument('--fps', type=int, default=30, help='Frames per second for output videos')
    parser.add_argument('--delete', type=str, choices=['yes', 'no'], default='no',
                        help='Delete working directory without verification prompt (yes/no)')
    parser.add_argument('--working_dir_name', type=str, default='working_dir',
                        help='Base directory name for working directories')
    parser.add_argument('--video_path_template', type=str, default='./VideoInputs/Video{}.mp4',
                        help='Template path for video files, e.g., ./VideoInputs/Video{}.mp4')
    parser.add_argument('--images_extract_dir', type=str, default='./working_dir/images',
                        help='Directory to extract images')
    parser.add_argument('--temp_processing_dir', type=str, default='./working_dir/temp',
                        help='Directory for temporary processing images')
    parser.add_argument('--rendered_dir', type=str, default='./working_dir/render',
                        help='Directory for rendered mask outputs')
    parser.add_argument('--overlap_dir', type=str, default='./working_dir/overlap',
                        help='Directory for overlapped images')
    parser.add_argument('--verified_img_dir', type=str, default='./working_dir/verified/images',
                        help='Directory for verified original images')
    parser.add_argument('--verified_mask_dir', type=str, default='./working_dir/verified/mask',
                        help='Directory for verified mask images')
    parser.add_argument('--final_video_path', type=str, default='./outputs',
                        help='Directory to save output videos')

    args = parser.parse_args()

    for i in range(args.video_start, args.video_start + args.video_end):
        if os.path.exists(args.working_dir_name):
            if args.delete.lower() == 'yes':
                shutil.rmtree(args.working_dir_name)
                logger.info(f"Cleared prev working directory: {args.working_dir_name}")
            else:
                confirm = input(
                    f"Do you want to clear prev working directory '{args.working_dir_name}'? (yes/no): "
                ).lower()
                if confirm == 'yes':
                    shutil.rmtree(args.working_dir_name)
                    logger.info(f"Clearing prev working directory: {args.working_dir_name}")
                else:
                    logger.info(f"Working directory '{args.working_dir_name}' not deleted")
                    logger.info('Stopping the process')
                    sys.exit(1000)

        run_pipeline(
            fps=args.fps,
            video_number=i,
            prefix=args.prefix,
            batch_size=args.batch_size,
            delete=args.delete.lower(),
            video_path_template=args.video_path_template.replace('working_dir', args.working_dir_name),
            images_extract_dir=args.images_extract_dir.replace('working_dir', args.working_dir_name),
            temp_processing_dir=args.temp_processing_dir.replace('working_dir', args.working_dir_name),
            rendered_dirs=args.rendered_dir.replace('working_dir', args.working_dir_name),
            overlap_dir=args.overlap_dir.replace('working_dir', args.working_dir_name),
            verified_img_dir=args.verified_img_dir.replace('working_dir', args.working_dir_name),
            verified_mask_dir=args.verified_mask_dir.replace('working_dir', args.working_dir_name),
            final_video_path=args.final_video_path
        )

        logger.info("Pipeline completed for all videos.")
        if os.path.exists(args.working_dir_name):
            if args.delete.lower() == 'yes':
                shutil.rmtree(args.working_dir_name)
                logger.info(f"Cleared working directory: {args.working_dir_name}")
            else:
                confirm = input(
                    f"Are you sure you want to delete the working directory '{args.working_dir_name}'? (yes/no): "
                ).lower()
                if confirm == 'yes':
                    shutil.rmtree(args.working_dir_name)
                    logger.info(f"Cleared working directory: {args.working_dir_name}")
                else:
                    logger.info(f"Working directory '{args.working_dir_name}' not deleted")


if __name__ == "__main__":
    main()

'''
pyinstaller --name sam2_video_predictor --add-data "../checkpoints\sam2_hiera_large.pt;checkpoints" --add-data "../sam2_co
nfigs\sam2_hiera_l.yaml;sam2_configs" --add-data "../sam2_configs;sam2_configs" --hidden-import torch --hidden-import cv2 --hidden-import numpy --hidden-import GPUtil --hidden-import sam2 --hidden-import sam2.sam2_configs --collect-all sam2 --onefile sam2_video_predictor.py

'''
