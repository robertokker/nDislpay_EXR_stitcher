import os
import sys
import json
import glob
import re
import concurrent.futures
import traceback

import OpenEXR
import Imath

try:
    import OpenImageIO as oiio
    HAS_OIIO = True
except ImportError:
    HAS_OIIO = False

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog, QSpinBox,
    QTextEdit, QProgressBar, QMessageBox, QGroupBox, QGridLayout,
    QCheckBox, QRadioButton, QComboBox
)
from PySide6.QtCore import Qt, QThread, Signal

# --- Multiprocessing Functions ---

def _box2i_from_tuple(b_tuple):
    """Convert (x_min, y_min, x_max, y_max) to Imath.Box2i"""
    return Imath.Box2i(
        Imath.V2i(b_tuple[0], b_tuple[1]),
        Imath.V2i(b_tuple[2], b_tuple[3])
    )

def process_frame_task(task_data):
    """
    Metadata Patch task function executed in a separate process.
    """
    in_path = task_data['input_path']
    out_path = task_data['output_path']
    display_win_tuple = task_data['display_window']
    data_win_tuple = task_data['data_window']
    viewport = task_data['viewport']

    try:
        in_file = OpenEXR.InputFile(in_path)
        header = in_file.header()
        
        header['displayWindow'] = _box2i_from_tuple(display_win_tuple)
        header['dataWindow'] = _box2i_from_tuple(data_win_tuple)
        
        channels = header['channels'].keys()
        pixel_data = in_file.channels(channels)
        
        in_file.close()
        
        out_file = OpenEXR.OutputFile(out_path, header)
        pixel_dict = dict(zip(channels, pixel_data))
        out_file.writePixels(pixel_dict)
        out_file.close()
        
        return (True, viewport, in_path, "Success")
    except Exception as e:
        return (False, viewport, in_path, str(e))

def stitch_frame_task(task_data):
    """
    OpenImageIO Stitch task function executed in a separate process.
    """
    if not HAS_OIIO:
        return (False, f"Frame {task_data['frame_num']}", "", "OpenImageIO is not installed.")
        
    # Limit internal OIIO threads per process to prevent thread explosion & excessive memory alloc
    oiio.attribute("threads", 1)
    
    out_path = task_data['output_path']
    frame_num = task_data['frame_num']
    viewports = task_data['viewports']
    
    try:
        # Open first file to inherit spec
        first_vp = viewports[0]
        first_buf = oiio.ImageBuf(first_vp['filepath'])
        if first_buf.has_error:
            return (False, f"Frame {frame_num}", "", f"Error reading {first_vp['filepath']}: {first_buf.geterror()}")
            
        spec = first_buf.spec()
        
        # Modify spec for master canvas
        spec.width = task_data['global_w']
        spec.height = task_data['global_h']
        spec.full_width = task_data['global_w']
        spec.full_height = task_data['global_h']
        spec.x = 0
        spec.y = 0
        spec.full_x = 0
        spec.full_y = 0
        
        if task_data['compression'] != "none":
            spec.attribute("compression", task_data['compression'])
            
        # Initialize Master Buffer
        master_buf = oiio.ImageBuf(spec)
        
        # Paste viewports
        for vp in viewports:
            vp_buf = oiio.ImageBuf(vp['filepath'])
            if vp_buf.has_error:
                continue 
            oiio.ImageBufAlgo.paste(master_buf, vp['x'], vp['y'], 0, 0, vp_buf)
            vp_buf.clear()
            del vp_buf
            
        # Write output
        master_buf.write(out_path)
        master_buf.clear()
        first_buf.clear()
        
        # Explicitly delete to force garbage collection of large C++ buffers
        del master_buf
        del first_buf
        
        return (True, f"Frame {frame_num}", out_path, "Success")
    except Exception as e:
        return (False, f"Frame {frame_num}", "", str(e))

# --- Worker Thread for UI ---

class PatchWorker(QThread):
    progress_signal = Signal(int, int) # completed, total
    log_signal = Signal(str)
    finished_signal = Signal()

    def __init__(self, tasks, max_workers=None, parent=None):
        super().__init__(parent)
        self.tasks = tasks
        self.max_workers = max_workers

    def run(self):
        if not self.tasks:
            self.finished_signal.emit()
            return
            
        total_tasks = len(self.tasks)
        self.log_signal.emit(f"Starting to process {total_tasks} jobs...")
        
        completed = 0
        
        mode = self.tasks[0].get('mode', 'patch')
        task_func = process_frame_task if mode == 'patch' else stitch_frame_task
        
        with concurrent.futures.ProcessPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(task_func, task): task for task in self.tasks}
            
            for future in concurrent.futures.as_completed(futures):
                success, identifier, filepath, msg = future.result()
                filename = os.path.basename(filepath)
                
                if success:
                    self.log_signal.emit(f"[OK] [{identifier}] {filename}")
                else:
                    self.log_signal.emit(f"[ERROR] [{identifier}] {filename} - {msg}")
                
                completed += 1
                self.progress_signal.emit(completed, total_tasks)
                
        self.log_signal.emit("Processing complete.")
        self.finished_signal.emit()

# --- Main UI Application ---

class NDisplayPatcherUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("nDisplay EXR Utility")
        self.resize(750, 650)
        
        self.worker = None
        self.setup_ui()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        
        # --- Mode Group ---
        mode_group = QGroupBox("Processing Mode")
        mode_layout = QHBoxLayout(mode_group)
        
        self.radio_patch = QRadioButton("Metadata Patch (Fast)")
        self.radio_patch.setChecked(True)
        self.radio_patch.setToolTip("Modify EXR headers without altering pixel data (Extremely fast).")
        self.radio_stitch = QRadioButton("Full Stitch (OIIO)")
        self.radio_stitch.setToolTip("Physically merge multiple viewports into a single EXR per frame using OpenImageIO.")
        mode_layout.addWidget(self.radio_patch)
        mode_layout.addWidget(self.radio_stitch)
        
        mode_layout.addSpacing(20)
        mode_layout.addWidget(QLabel("Stitch Compression:"))
        self.combo_comp = QComboBox()
        self.combo_comp.addItems(["zip", "dwaa", "none", "rle", "zips", "piz"])
        self.combo_comp.setEnabled(False)
        self.combo_comp.setToolTip("Select the compression format for the stitched output. 'zip' is lossless, 'dwaa' is lossy and much smaller.")
        mode_layout.addWidget(self.combo_comp)
        
        mode_layout.addSpacing(20)
        mode_layout.addWidget(QLabel("Max Workers:"))
        self.spin_workers = QSpinBox()
        self.spin_workers.setRange(1, 64)
        self.spin_workers.setValue(4)
        self.spin_workers.setEnabled(False)
        self.spin_workers.setToolTip("Limit the number of parallel processes to prevent running out of RAM, especially with massive EXRs.")
        mode_layout.addWidget(self.spin_workers)
        
        mode_layout.addStretch()
        main_layout.addWidget(mode_group)
        
        self.radio_patch.toggled.connect(self.on_mode_changed)
        self.radio_stitch.toggled.connect(self.on_mode_changed)
        
        # --- Config Group ---
        config_group = QGroupBox("Configuration")
        config_layout = QGridLayout(config_group)
        config_layout.addWidget(QLabel("nDisplay Config (.json):"), 0, 0)
        self.json_line = QLineEdit()
        self.json_line.setToolTip("Path to the .ndisplay configuration JSON file.")
        config_layout.addWidget(self.json_line, 0, 1)
        btn_json = QPushButton("Browse...")
        btn_json.clicked.connect(self.browse_json)
        config_layout.addWidget(btn_json, 0, 2)
        main_layout.addWidget(config_group)
        
        # --- Paths Group ---
        paths_group = QGroupBox("Paths")
        paths_layout = QGridLayout(paths_group)
        
        paths_layout.addWidget(QLabel("Input Folder:"), 0, 0)
        self.input_line = QLineEdit()
        self.input_line.setToolTip("Directory containing the original raw nDisplay EXR sequence.")
        self.input_line.textChanged.connect(self.on_input_text_changed)
        paths_layout.addWidget(self.input_line, 0, 1)
        btn_input = QPushButton("Browse...")
        btn_input.clicked.connect(self.browse_input)
        paths_layout.addWidget(btn_input, 0, 2)
        
        self.chk_overwrite = QCheckBox("Overwrite Input Files In-Place")
        self.chk_overwrite.setToolTip("Patch files directly in the input directory without creating a copy. (Disabled for Full Stitch)")
        self.chk_overwrite.toggled.connect(self.toggle_overwrite)
        paths_layout.addWidget(self.chk_overwrite, 1, 1, 1, 2)
        
        paths_layout.addWidget(QLabel("Output Folder:"), 2, 0)
        self.output_line = QLineEdit()
        self.output_line.setToolTip("Directory to save the processed sequence.")
        paths_layout.addWidget(self.output_line, 2, 1)
        self.btn_output = QPushButton("Browse...")
        self.btn_output.clicked.connect(self.browse_output)
        paths_layout.addWidget(self.btn_output, 2, 2)
        
        main_layout.addWidget(paths_group)
        
        # --- Frame Controls Group ---
        frames_group = QGroupBox("Frame Controls")
        frames_layout = QHBoxLayout(frames_group)
        
        frames_layout.addWidget(QLabel("Start Frame:"))
        self.spin_start = QSpinBox()
        self.spin_start.setRange(0, 9999999)
        self.spin_start.setToolTip("First frame of the sequence to process.")
        frames_layout.addWidget(self.spin_start)
        
        frames_layout.addWidget(QLabel("End Frame:"))
        self.spin_end = QSpinBox()
        self.spin_end.setRange(0, 9999999)
        self.spin_end.setToolTip("Last frame of the sequence to process.")
        frames_layout.addWidget(self.spin_end)
        
        frames_layout.addWidget(QLabel("Frame Step:"))
        self.spin_step = QSpinBox()
        self.spin_step.setRange(1, 1000)
        self.spin_step.setValue(1)
        self.spin_step.setToolTip("Process every Nth frame.")
        frames_layout.addWidget(self.spin_step)
        
        frames_layout.addStretch()
        main_layout.addWidget(frames_group)
        
        # --- Action Buttons ---
        buttons_layout = QHBoxLayout()
        self.btn_analyze = QPushButton("Analyze Sequence")
        self.btn_analyze.setToolTip("Check if all expected viewports and frames are present before processing.")
        self.btn_analyze.setMinimumHeight(40)
        self.btn_analyze.clicked.connect(self.analyze_sequence)
        buttons_layout.addWidget(self.btn_analyze)
        
        self.btn_process = QPushButton("Process Frames")
        self.btn_process.setToolTip("Start the patching or stitching operation.")
        self.btn_process.setMinimumHeight(40)
        self.btn_process.clicked.connect(self.start_processing)
        buttons_layout.addWidget(self.btn_process)
        
        main_layout.addLayout(buttons_layout)
        
        # --- Log & Progress ---
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        main_layout.addWidget(self.log_text)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        main_layout.addWidget(self.progress_bar)

    def on_mode_changed(self):
        is_stitch = self.radio_stitch.isChecked()
        self.combo_comp.setEnabled(is_stitch)
        self.spin_workers.setEnabled(is_stitch)
        
        if is_stitch:
            self.chk_overwrite.setChecked(False)
            self.chk_overwrite.setEnabled(False)
            self.output_line.setEnabled(True)
            self.btn_output.setEnabled(True)
        else:
            self.chk_overwrite.setEnabled(True)
            self.toggle_overwrite(self.chk_overwrite.isChecked())

    def toggle_overwrite(self, checked):
        if not self.radio_stitch.isChecked():
            self.output_line.setEnabled(not checked)
            self.btn_output.setEnabled(not checked)

    def browse_input(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Input Folder")
        if folder:
            self.input_line.setText(folder)

    def on_input_text_changed(self, text):
        if os.path.isdir(text):
            self.auto_detect_frame_range(text)

    def auto_detect_frame_range(self, folder):
        try:
            files = os.listdir(folder)
            frame_pattern = re.compile(r'(?:_|\.)(\d{1,8})\.exr$', re.IGNORECASE)
            frames = []
            for f in files:
                match = frame_pattern.search(f)
                if match:
                    frames.append(int(match.group(1)))
            
            if frames:
                min_f = min(frames)
                max_f = max(frames)
                self.spin_start.setValue(min_f)
                self.spin_end.setValue(max_f)
                self.log(f"Auto-detected frame range: {min_f} - {max_f}")
        except Exception as e:
            pass 

    def browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder")
        if folder:
            self.output_line.setText(folder)

    def browse_json(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select nDisplay Config", "", "JSON Files (*.json *.ndisplay)")
        if file_path:
            self.json_line.setText(file_path)

    def log(self, message):
        self.log_text.append(message)

    def find_key_recursive(self, data, target_key):
        if isinstance(data, dict):
            if target_key in data:
                return data[target_key]
            for k, v in data.items():
                res = self.find_key_recursive(v, target_key)
                if res is not None:
                    return res
        elif isinstance(data, list):
            for item in data:
                res = self.find_key_recursive(item, target_key)
                if res is not None:
                    return res
        return None

    def get_config_viewports(self):
        json_path = self.json_line.text().strip()
        if not os.path.exists(json_path):
            QMessageBox.warning(self, "Invalid Path", "nDisplay Config file does not exist.")
            return None
            
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "JSON Error", f"Failed to read JSON: {e}")
            return None
            
        cluster = self.find_key_recursive(config_data, 'cluster')
        if not cluster:
            QMessageBox.critical(self, "Config Error", "Could not find 'cluster' section in JSON.")
            return None
            
        nodes = cluster.get('nodes', {})
        if not nodes:
            QMessageBox.critical(self, "Config Error", "Could not find 'nodes' section in JSON.")
            return None

        viewport_data = {}
        for node_name, node_data in nodes.items():
            win = node_data.get('window', {})
            node_w = int(win.get('w', 0))
            node_h = int(win.get('h', 0))
            
            viewports = node_data.get('viewports', {})
            for vp_name, vp_info in viewports.items():
                if 'region' in vp_info:
                    reg = vp_info['region']
                    viewport_data[vp_name] = {
                        'x': int(reg.get('x', 0)),
                        'y': int(reg.get('y', 0)),
                        'w': int(reg.get('w', 0)),
                        'h': int(reg.get('h', 0)),
                        'node_name': node_name,
                        'node_w': node_w,
                        'node_h': node_h
                    }
                    
        if not viewport_data:
            QMessageBox.critical(self, "Config Error", "No valid viewports with 'region' found in JSON.")
            return None
            
        return viewport_data

    def scan_tasks(self, in_folder, viewport_data, start_frame, end_frame, step):
        files = os.listdir(in_folder)
        frame_pattern = re.compile(r'(?:_|\.)(\d{1,8})\.exr$', re.IGNORECASE)
        
        found_files = []
        
        for f in files:
            if not f.lower().endswith('.exr'):
                continue
                
            match = frame_pattern.search(f)
            if match:
                frame_num = int(match.group(1))
                if not (start_frame <= frame_num <= end_frame):
                    continue
                if step > 1 and (frame_num - start_frame) % step != 0:
                    continue
            else:
                continue
                
            matched_vp = None
            sorted_vp_names = sorted(viewport_data.keys(), key=len, reverse=True)
            for vp_name in sorted_vp_names:
                if vp_name in f:
                    matched_vp = vp_name
                    break
                    
            if matched_vp:
                found_files.append((f, matched_vp, frame_num))
                
        return found_files

    def analyze_sequence(self):
        in_folder = self.input_line.text().strip()
        if not in_folder or not os.path.exists(in_folder):
            QMessageBox.warning(self, "Invalid Path", "Input folder does not exist.")
            return False

        viewport_data = self.get_config_viewports()
        if not viewport_data:
            return False
            
        start_frame = self.spin_start.value()
        end_frame = self.spin_end.value()
        step = self.spin_step.value()
        
        self.log("--- Analyzing Sequence ---")
        self.log(f"Expected Viewports: {len(viewport_data)}")
        
        found_files = self.scan_tasks(in_folder, viewport_data, start_frame, end_frame, step)
        
        expected_frames = list(range(start_frame, end_frame + 1, step))
        found_frames = {vp: [] for vp in viewport_data.keys()}
        
        for f, vp, frame_num in found_files:
            found_frames[vp].append(frame_num)
            
        missing_vps = []
        incomplete_vps = []
        
        for vp, f_list in found_frames.items():
            if not f_list:
                missing_vps.append(vp)
            else:
                missing = set(expected_frames) - set(f_list)
                if missing:
                    incomplete_vps.append((vp, len(missing)))
        
        has_warnings = False
        if missing_vps:
            self.log(f"[WARNING] Missing entirely ({len(missing_vps)} viewports): {', '.join(missing_vps)}")
            has_warnings = True
        else:
            self.log("[OK] All expected viewports found in folder.")
            
        if incomplete_vps:
            for vp, count in incomplete_vps:
                self.log(f"[WARNING] Viewport '{vp}' is missing {count} frames.")
            has_warnings = True
        elif not missing_vps:
            self.log("[OK] All viewports have complete frame ranges.")
            
        self.log(f"Analysis complete. Found {len(found_files)} total files.")
        self.log("------------------------")
        
        if has_warnings:
            reply = QMessageBox.question(
                self, 'Analysis Warnings',
                "Some viewports or frames are missing. Check the log for details.\n\nDo you want to continue processing anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            return reply == QMessageBox.Yes
        else:
            QMessageBox.information(self, "Analysis Complete", "All viewports and frames are present!")
            return True

    def start_processing(self):
        in_folder = self.input_line.text().strip()
        out_folder = self.output_line.text().strip()
        is_stitch = self.radio_stitch.isChecked()
        is_overwrite = self.chk_overwrite.isChecked() and not is_stitch
        
        if is_stitch and not HAS_OIIO:
            QMessageBox.critical(self, "Missing Dependency", "OpenImageIO is not installed. Please run 'pip install OpenImageIO' to use Full Stitch mode.")
            return

        if not is_overwrite and not out_folder:
            QMessageBox.warning(self, "Missing Fields", "Please specify an Output Folder.")
            return
            
        if not os.path.exists(in_folder):
            QMessageBox.warning(self, "Invalid Path", "Input folder does not exist.")
            return

        if is_stitch and os.path.normpath(in_folder) == os.path.normpath(out_folder):
            QMessageBox.critical(self, "Invalid Path", "Input and Output folders must be DIFFERENT for Full Stitch Mode to avoid overwriting raw renders.")
            return
            
        viewport_data = self.get_config_viewports()
        if not viewport_data:
            return
            
        start_frame = self.spin_start.value()
        end_frame = self.spin_end.value()
        step = self.spin_step.value()
        
        found_files = self.scan_tasks(in_folder, viewport_data, start_frame, end_frame, step)
        
        if not found_files:
            QMessageBox.warning(self, "No Files", "No EXR files matched the viewports and frame range.")
            return
            
        if not is_overwrite:
            os.makedirs(out_folder, exist_ok=True)
        else:
            out_folder = in_folder
            
        tasks = []
        
        if is_stitch:
            frames_dict = {}
            for f, vp, frame_num in found_files:
                node_name = viewport_data[vp]['node_name']
                key = (frame_num, node_name)
                if key not in frames_dict:
                    frames_dict[key] = []
                frames_dict[key].append((f, vp))
                
            for (frame_num, node_name), files_list in frames_dict.items():
                vps_list = []
                node_w = viewport_data[files_list[0][1]]['node_w']
                node_h = viewport_data[files_list[0][1]]['node_h']
                for f, vp in files_list:
                    vp_info = viewport_data[vp]
                    vps_list.append({
                        'filepath': os.path.join(in_folder, f),
                        'viewport': vp,
                        'x': vp_info['x'],
                        'y': vp_info['y']
                    })
                
                first_f = files_list[0][0]
                first_vp = files_list[0][1]
                stitched_name = first_f.replace(f"_{first_vp}", f"_{node_name}")
                if stitched_name == first_f:
                    stitched_name = first_f.replace(first_vp, f"Stitched_{node_name}")
                
                out_path = os.path.join(out_folder, stitched_name)
                
                tasks.append({
                    'mode': 'stitch',
                    'frame_num': frame_num,
                    'output_path': out_path,
                    'viewports': vps_list,
                    'global_w': node_w,
                    'global_h': node_h,
                    'compression': self.combo_comp.currentText()
                })
        else:
            for f, vp, frame_num in found_files:
                vp_info = viewport_data[vp]
                display_win_tuple = (0, 0, vp_info['node_w'] - 1, vp_info['node_h'] - 1)
                data_win_tuple = (
                    vp_info['x'],
                    vp_info['y'],
                    vp_info['x'] + vp_info['w'] - 1,
                    vp_info['y'] + vp_info['h'] - 1
                )
                
                in_path = os.path.join(in_folder, f)
                out_path = os.path.join(out_folder, f)
                
                tasks.append({
                    'mode': 'patch',
                    'input_path': in_path,
                    'output_path': out_path,
                    'display_window': display_win_tuple,
                    'data_window': data_win_tuple,
                    'viewport': vp
                })
            
        self.log(f"Starting processing of {len(tasks)} jobs...")
        
        self.btn_process.setEnabled(False)
        self.btn_analyze.setEnabled(False)
        self.progress_bar.setMaximum(len(tasks))
        self.progress_bar.setValue(0)
        
        max_workers = self.spin_workers.value()
        self.worker = PatchWorker(tasks, max_workers=max_workers)
        self.worker.progress_signal.connect(self.update_progress)
        self.worker.log_signal.connect(self.log)
        self.worker.finished_signal.connect(self.processing_finished)
        self.worker.start()

    def update_progress(self, completed, total):
        self.progress_bar.setValue(completed)

    def processing_finished(self):
        self.btn_process.setEnabled(True)
        self.btn_analyze.setEnabled(True)
        QMessageBox.information(self, "Complete", "Processing completed.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = NDisplayPatcherUI()
    window.show()
    sys.exit(app.exec())
