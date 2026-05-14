# nDisplay EXR Metadata Patcher

A standalone PySide6 Python utility designed to automatically patch OpenEXR header metadata (`displayWindow` and `dataWindow`) for Unreal Engine nDisplay render sequences.

## Features

- **Config Parsing**: Reads complex nDisplay JSON configurations (`.json` or `.ndisplay`) to extract global cluster resolution and specific viewport regions.
- **Sequence Analysis**: Scans input directories to auto-detect frame ranges, matching files to viewports, and warns about missing frames or incomplete sequences.
- **High Performance**: Utilizes multiprocessing (`ProcessPoolExecutor`) for fast, parallel patching of EXR files.
- **Flexible Output**: Supports both in-place EXR overwriting and exporting to a new destination folder.
- **User Interface**: Provides an intuitive PySide6 GUI with controls for paths, frame ranges, and real-time progress/logging feedback.

## Requirements

- Python 3.x
- `PySide6`
- `OpenEXR`
- `Imath`

## Installation

1. Clone this repository:
   ```bash
   git clone <your-repository-url>
   cd nDislpay_EXR_stitcher
   ```
2. Install the required dependencies:
   ```bash
   pip install PySide6 OpenEXR Imath
   ```

*(Note: Depending on your OS, you may need pre-compiled binaries for OpenEXR and Imath if building from source fails).*

## Usage

Run the script using Python:
```bash
python ndisplay_exr_patcher.py
```

1. **Input Folder**: Select the folder containing your unpatched EXR sequences.
2. **nDisplay Config**: Select your `.ndisplay` or `.json` config file.
3. **Output Folder**: Select a destination, or check "Overwrite Input Files In-Place".
4. **Frame Controls**: Set your start, end, and step frames (auto-detected if you select the input folder).
5. **Analyze Sequence**: Click to verify that all expected viewports and frames are present.
6. **Process Frames**: Run the batch patcher.

## License

[MIT License](LICENSE)
