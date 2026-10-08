"""Write the Windows version resource (shown in the exe's Properties > Details) from autoreload.__version__."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from autoreload import __version__  # noqa: E402

parts = (__version__.split(".") + ["0", "0", "0"])[:4]
tup = ", ".join(parts)
text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', ''),
      StringStruct('FileDescription', 'Auto Reload'),
      StringStruct('FileVersion', '{__version__}'),
      StringStruct('InternalName', 'AutoReload'),
      StringStruct('OriginalFilename', 'AutoReload.exe'),
      StringStruct('ProductName', 'Auto Reload'),
      StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
out = Path(sys.argv[1])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(text, encoding="utf-8")
