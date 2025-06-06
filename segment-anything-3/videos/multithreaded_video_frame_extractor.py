import os
import shutil
from concurrent.futures import ThreadPoolExecutor

import cv2
from tqdm import tqdm

# Configuration
VIDEO_NUMBER = 9
NUM_THREADS = 16  # Number of threads for multithreading
VIDEO_PATH_TEMPLATE = r'../VideoInputs/video{}.mp4'
OUTPUT_DIR = r'./videos/road_imgs'


def clear_output_directory(directory):
    """Clear the output directory if it exists."""
    if os.path.exists(directory):
        for filename in os.listdir(directory):
            file_path = os.path.join(directory, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                print(f"Failed to delete {file_path}. Reason: {e}")


def save_frame(frame_info):
    """Save the individual frame as a .png file."""
    frame, frame_count, output_dir, video_number = frame_info
    frame_filename = os.path.join(output_dir, f'road{video_number}_{frame_count:05d}.png')
    cv2.imwrite(frame_filename, frame)


def extract_frames_in_range(start_frame, end_frame, video_path, output_dir, video_number, progress_bar):
    """Extract and save frames for a given range of frame numbers with a separate VideoCapture object."""
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    for frame_count in range(start_frame, end_frame):
        ret, frame = cap.read()
        if not ret:
            break
        # Save frame
        save_frame((frame, frame_count, output_dir, video_number))
        # Update progress bar
        progress_bar.update(1)

    cap.release()


def process_video_in_parallel(video_number, video_path, output_dir, num_threads):
    """Process the video by extracting frames in parallel using multiple threads."""
    # Load the video to get the total frame count
    cap = cv2.VideoCapture(video_path)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    cap.release()

    # Determine how many frames each thread will process
    frames_per_thread = total_frames // num_threads
    frame_ranges = [(i * frames_per_thread, (i + 1) * frames_per_thread if i != num_threads - 1 else total_frames)
                    for i in range(num_threads)]
    # frame_ranges = frame_ranges[:1]
    # Initialize the progress bar
    with tqdm(total=total_frames, desc='Extracting Frames', unit='frame') as progress_bar:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            # Submit each thread to extract frames in the respective range
            for start_frame, end_frame in frame_ranges:
                executor.submit(extract_frames_in_range, start_frame, end_frame, video_path, output_dir, video_number,
                                progress_bar)

    print(f"Extracted frames to '{output_dir}'")


def main(video_number=VIDEO_NUMBER):
    """Main function to manage the video processing."""
    # Define paths
    video_path = VIDEO_PATH_TEMPLATE.format(video_number)

    # Create output directory and clear any existing files
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    clear_output_directory(OUTPUT_DIR)

    # Start multithreaded frame extraction
    process_video_in_parallel(video_number, video_path, OUTPUT_DIR, NUM_THREADS)


if __name__ == "__main__":
    main(VIDEO_NUMBER)

'''
    video18 = road1 ==> 40
    video19 = road2 ==> 40
    video2  = road3 ==> 40
    video23 = road4 ==> 40

    video31 = road31 ==> 80
    video32 = road32 ==> 80
    video33 = road33 ==> 80
    video34 = road34 ==> 80
    video35 = road35 ==> 80
    video36 = road36 ==> 80
    video37 = road37 ==> 80
    video38 = road38 ==> 80
    video39 = road39 ==> 80
    video40 = road40 ==> 80
    video41 = road41 ==> 80
    video42 = road42 ==> 80
    video43 = road43 ==> 80
    video44 = road44 ==> 80
    video45 = road45 ==> 80
    video46 = road46 ==> 80
    video47 = road47 ==> 80
    video48 = road48 ==> 80
    video49 = road49 ==> 80
    video50 = road50 ==> 80
    video51 = road51 ==> 80
    video52 = road52 ==> 100

'''
