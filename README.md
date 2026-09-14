# Merge PDF

Desktop (Windows) application for combining two PDF files, with a tkinter GUI:
drag & drop folders, a sortable/filterable PDF file list, and a choice of
where the merge happens (beginning, end, or between pages).

## Features

- Two folder-selection zones - drag & drop or **Browse** button.
- List of PDF files in each folder, with name and last-modified date,
  sortable by clicking a column header.
- A search/filter field by file name for each list.
- **Auto-refresh**: each selected folder is rescanned automatically every
  2 seconds - new files that appear in the folder show up on their own,
  no need to Browse again.
- Select one file from Folder 1 (base file) and one from Folder 2
  (file to insert).
- Choose where the merge happens:
  - at the beginning of the base file
  - at the end of the base file
  - between pages, after a chosen page number
- Save the result through a standard "Save as" dialog.
- Clear success/error messages (empty folder, missing folder, corrupted
  file, invalid page number, etc.).
- Dark visual theme with teal (Folder 1) and neon red (Folder 2) accents,
  including a dark Windows title bar and a custom icon.

## Running

### From the executable (no Python required)

Run `dist/MergePDF.exe` directly.

### From source

Requires Python 3.10+.

```
pip install -r requirements.txt
python main.py
```

## Project structure

All application code lives in a single file, [main.py](main.py), organized
into commented sections:

1. **PDF logic** - folder scanning and the merge operation (`list_pdf_files`,
   `merge_pdfs`), with no dependency on tkinter.
2. **Visual theme** - the color palette and ttk styles (`configure_dark_theme`).
3. **`FolderPanel`** - the reusable widget for a folder panel (drag & drop,
   Browse, search, sortable list, auto-refresh).
4. **`MergeApp`** - the main window, connecting the two panels to the merge
   options and the **Merge** button.
5. **Entry point** (`main()`).

Other files in the project:

- `requirements.txt` - Python dependencies (`pypdf`, `tkinterdnd2`).
- `app_icon.ico` - the application icon (generated with Pillow).
- `MergePDF.spec` - the PyInstaller configuration used to build the executable.
- `dist/MergePDF.exe` - the built executable (no Python installation required).

## Rebuilding the executable

After making changes to `main.py`:

```
pip install pyinstaller
pyinstaller MergePDF.spec
```

The resulting executable appears in `dist/MergePDF.exe`.

## Dependencies

- [pypdf](https://pypi.org/project/pypdf/) - reading/writing/merging PDF
  files.
- [tkinterdnd2](https://pypi.org/project/tkinterdnd2/) - drag & drop support
  for tkinter.
- [Pillow](https://pypi.org/project/Pillow/) - used only to generate the
  application icon (not required for normal use).
