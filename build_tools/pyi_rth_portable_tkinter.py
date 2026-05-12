import os
import sys


base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
tcl_dir = os.path.join(base_dir, "_tcl_data")
tk_dir = os.path.join(base_dir, "_tk_data")

if os.path.isdir(tcl_dir):
    os.environ["TCL_LIBRARY"] = tcl_dir
if os.path.isdir(tk_dir):
    os.environ["TK_LIBRARY"] = tk_dir
