"""Double-click to open the DeepSeek Web API window.

Windows runs .pyw files with pythonw, so no console window opens. This only starts
deepseek_web_api.py (which must sit in the same folder) with the "gui" command.
"""

import os
import runpy
import sys

script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek_web_api.py")
sys.argv = [script, "gui"] + sys.argv[1:]
runpy.run_path(script, run_name="__main__")
