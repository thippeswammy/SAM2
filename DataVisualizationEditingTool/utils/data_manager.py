import os
import shutil
import time

import numpy as np


class DataManager:
    def __init__(self, data, file_names):
        if data.size > 0:
            if len(data.shape) != 2 or data.shape[1] != 6:
                raise ValueError(
                    f"Expected data to be a 2D array with 6 columns (x, y, yaw, frame_idx, index, lane_id), "
                    f"got shape {data.shape}"
                )
            if not np.issubdtype(data[:, -1].dtype, np.integer):
                data[:, -1] = data[:, -1].astype(int)
            if not np.issubdtype(data[:, 3].dtype, np.integer) or not np.issubdtype(data[:, 4].dtype, np.integer):
                data[:, 3] = data[:, 3].astype(int)
                data[:, 4] = data[:, 4].astype(int)
            if not np.array_equal(data[:, 3], data[:, 4]):
                raise ValueError("frame_idx and index columns must be identical")
            self.data = data.copy()
        else:
            self.data = np.array([])

        self.file_names = file_names
        self.total_cols = data.shape[1] if data.size > 0 else 0
        self.history = [self.data.copy()] if self.data.size > 0 else [np.array([])]
        self.redo_stack = []
        self.last_backup = time.time()
        self.backup_interval = 300  # 5 minutes

        print(f"DataManager initialized with {len(self.data)} points")

    def add_point(self, x, y, lane_id):
        try:
            new_point = np.zeros((1, self.total_cols))
            new_point[0, 0] = x
            new_point[0, 1] = y
            new_point[0, -1] = lane_id
            new_index = len(self.data)
            new_point[0, 3] = new_index
            new_point[0, 4] = new_index
            self.data = np.vstack([self.data, new_point]) if self.data.size > 0 else new_point
            self.history.append(self.data.copy())
            self.redo_stack = []
            self._auto_save_backup()
            print(f"Added point: ({x:.2f}, {y:.2f}, lane_id={lane_id})")
        except Exception as e:
            print(f"Error adding point: {e}")

    def delete_points(self, indices):
        """Delete points at specified indices from the data array."""
        if not indices:
            return
        try:
            # Validate indices
            indices = np.asarray(indices, dtype=int)
            if np.any(indices < 0) or np.any(indices >= len(self.data)):
                print(f"Error: Indices {indices} out of bounds for data of length {len(self.data)}")
                return
            mask = np.ones(len(self.data), dtype=bool)
            mask[indices] = False
            self.data = self.data[mask]
            # Handle empty data case
            if self.data.size == 0:
                self.data = np.array([], dtype=self.data.dtype).reshape(0, self.total_cols)
            else:
                new_indices = np.arange(len(self.data))
                self.data[:, 3] = new_indices
                self.data[:, 4] = new_indices
            self.history.append(self.data.copy())
            self.redo_stack = []
            self._auto_save_backup()
            print(f"Deleted {len(indices)} points")
        except Exception as e:
            print(f"Error deleting points: {e}")

    def change_ids(self, indices, new_id):
        if not indices:
            return
        try:
            self.data[indices, -1] = new_id
            self.history.append(self.data.copy())
            self.redo_stack = []
            self._auto_save_backup()
            print(f"Changed lane IDs for {len(indices)} points to {new_id}")
        except Exception as e:
            print(f"Error changing IDs: {e}")

    def remove_points_above(self, index, lane_id):
        """Remove points in the specified lane with local indices >= the local index of the given global index."""
        try:
            # Validate index and lane
            if index < 0 or index >= len(self.data) or int(self.data[index, -1]) != lane_id:
                print(f"Invalid index {index} or lane mismatch for lane {lane_id}")
                return
            lane_mask = self.data[:, -1] == lane_id
            lane_indices = np.where(lane_mask)[0]
            if len(lane_indices) == 0:
                print(f"No points found in lane {lane_id}")
                return
            lane_data = self.data[lane_mask]
            # Check for duplicate indices
            indices = lane_data[:, 4]
            if len(indices) != len(np.unique(indices)):
                print(f"Warning: Duplicate indices found in lane {lane_id}: {indices}")
            # Sort by index column (data[:, 4])
            sorted_indices = lane_data[:, 4].argsort()
            sorted_global_indices = lane_indices[sorted_indices]
            # Find the local index of the clicked point
            target_global_idx = np.where(sorted_global_indices == index)[0]
            if len(target_global_idx) == 0:
                print(f"Global index {index} not found in lane {lane_id}")
                return
            if len(target_global_idx) > 1:
                print(f"Warning: Multiple matches for global index {index} in lane {lane_id}, using first match")
            local_index = target_global_idx[0]
            indices_to_remove = sorted_global_indices[:local_index]
            list_ = []
            for i in indices_to_remove:
                list_.append(i)
            self.delete_points(list_)
            print(
                f"Removed {len(indices_to_remove)} points above local index {local_index} (global index {index}) in lane {lane_id}")
        except Exception as e:
            print(f"Error removing points above: {e}")

    def remove_points_below(self, index, lane_id):
        """Remove points in the specified lane with global indices <= the given index."""
        try:
            # Validate index
            if index < 0 or index >= len(self.data):
                print(f"Invalid index {index}")
                return
            # Filter points by lane_id
            lane_mask = self.data[:, -1] == lane_id
            lane_indices = np.where(lane_mask)[0]
            if len(lane_indices) == 0:
                print(f"No points found in lane {lane_id}")
                return
            # Select points with global indices <= index
            indices_to_remove = lane_indices[lane_indices >= index]
            # print('indices_to_remove=>', indices_to_remove)
            if len(indices_to_remove) == 0:
                print(f"No points with global indices <= {index} in lane {lane_id}")
                return
            list_ = []
            for i in indices_to_remove:
                list_.append(i)
            self.delete_points(list_)
            print(f"Removed {len(indices_to_remove)} points below index {index} in lane {lane_id}")
        except Exception as e:
            print(f"Error removing points below: {e}")

    def merge_lanes(self, lane_id_1, lane_id_2, point_1, point_2, point_1_type, point_2_type):
        if self.data.size == 0:
            print("No data to merge")
            return
        try:
            lane_1_mask = self.data[:, -1] == lane_id_1
            lane_2_mask = self.data[:, -1] == lane_id_2
            if not np.any(lane_1_mask) or not np.any(lane_2_mask):
                print(f"One or both lanes ({lane_id_1}, {lane_id_2}) are empty")
                return

            lane_1_data = self.data[lane_1_mask]
            lane_2_data = self.data[lane_2_mask]
            lane_1_indices = np.where(lane_1_mask)[0]
            lane_2_indices = np.where(lane_2_mask)[0]

            lane_1_sorted = lane_1_data[np.argsort(lane_1_data[:, 4])]
            lane_2_sorted = lane_2_data[np.argsort(lane_2_data[:, 4])]

            point_1_local = np.where(lane_1_indices == point_1)[0][0]
            point_2_local = np.where(lane_2_indices == point_2)[0][0]

            if point_1_type == 'end':
                lane_1_part = lane_1_sorted[:point_1_local + 1]
            else:
                lane_1_part = lane_1_sorted[point_1_local:]

            if point_2_type == 'start' and point_1_type == 'end':
                lane_2_part = lane_2_sorted
            elif point_2_type == 'end' and point_1_type == 'start':
                lane_2_part = lane_2_sorted
            elif point_2_type == 'start' and point_1_type == 'start':
                lane_2_part = lane_2_sorted[::-1]
            elif point_2_type == 'end' and point_1_type == 'end':
                lane_2_part = lane_2_sorted[::-1]

            merged_data = np.vstack([lane_1_part, lane_2_part])
            merged_data[:, -1] = lane_id_1

            N = len(merged_data)
            for i in range(N - 1):
                dx = merged_data[i + 1, 0] - merged_data[i, 0]
                dy = merged_data[i + 1, 1] - merged_data[i, 1]
                merged_data[i, 2] = np.arctan2(dy, dx)
            merged_data[-1, 2] = merged_data[-2, 2] if N > 1 else 0.0

            new_indices = np.arange(N)
            merged_data[:, 3] = new_indices
            merged_data[:, 4] = new_indices

            other_lanes_mask = ~np.logical_or(lane_1_mask, lane_2_mask)
            other_data = self.data[other_lanes_mask]
            self.data = np.vstack([merged_data, other_data]) if other_data.size > 0 else merged_data

            self.file_names = [self.file_names[i] if i < len(self.file_names) else f"Lane_{i}" for i in
                               range(max(np.unique(self.data[:, -1]).astype(int)) + 1)]
            self.history.append(self.data.copy())
            self.redo_stack = []
            self._auto_save_backup()
            print(f"Merged lane {lane_id_2} into lane {lane_id_1}")
        except Exception as e:
            print(f"Error merging lanes: {e}")

    def save_all_lanes(self):
        folder = "workspace-Temp"
        try:
            os.makedirs(folder, exist_ok=True)
            for filename in os.listdir(folder):
                file_path = os.path.join(folder, filename)
                try:
                    if os.path.isfile(file_path) or os.path.islink(file_path):
                        os.unlink(file_path)
                    elif os.path.isdir(file_path):
                        shutil.rmtree(file_path)
                except Exception as e:
                    print(f"Failed to delete {file_path}: {e}")
            unique_lane_ids = np.unique(self.data[:, -1])
            for lane_id in unique_lane_ids:
                mask = self.data[:, -1] == lane_id
                lane_data = self.data[mask]
                if lane_data.size > 0:
                    filename = os.path.join(folder, f"Lane_{int(lane_id)}.npy")
                    np.save(filename, lane_data[:, :3])
                    print(f"Saved lane {lane_id} to {filename}")
                else:
                    print(f"No data for lane {lane_id}, skipping save")
        except Exception as e:
            print(f"Error saving lanes: {e}")

    def clear_data(self):
        try:
            self.data = np.array([])
            self.history = [np.array([])]
            self.redo_stack = []
            self.file_names = []
            self._auto_save_backup()
            print("Cleared all data")
        except Exception as e:
            print(f"Error clearing data: {e}")

    def undo(self):
        try:
            if len(self.history) <= 1:
                print("Nothing to undo")
                return self.data, False
            self.redo_stack.append(self.history.pop())
            self.data = self.history[-1].copy()
            self._auto_save_backup()
            return self.data, True
        except Exception as e:
            print(f"Error during undo: {e}")
            return self.data, False

    def redo(self):
        try:
            if not self.redo_stack:
                print("Nothing to redo")
                return self.data, False
            self.data = self.redo_stack.pop()
            self.history.append(self.data.copy())
            self._auto_save_backup()
            return self.data, True
        except Exception as e:
            print(f"Error during redo: {e}")
            return self.data, False

    def save(self):
        try:
            filename = "WorkingLane.npy"
            if self.data.size > 0:
                np.save(filename, self.data[:, :3])
            else:
                np.save(filename, np.array([]))
            print(f"Saved x, y, yaw to {filename}")
            self._auto_save_backup()
            return filename
        except Exception as e:
            print(f"Error saving data: {e}")
            return None

    def _auto_save_backup(self):
        try:
            if time.time() - self.last_backup < self.backup_interval:
                return
            os.makedirs("workspace-Backup", exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = os.path.join("workspace-Backup", f"backup_{timestamp}.npy")
            if self.data.size > 0:
                np.save(filename, self.data[:, :3])
                print(f"Auto-saved backup to {filename}")
            self.last_backup = time.time()
        except Exception as e:
            print(f"Backup failed: {e}")
