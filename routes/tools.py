"""routes/tools.py: loads the three tools. app.py imports tools_bp from here, so nothing else has to change.

  routes/tools_common.py  shared pieces + the blueprint
  routes/answer_tool.py   Answer Finder
  routes/tracer_tool.py   Source Tracer
  routes/topic_tool.py    Topic Finder

Each tool is loaded on its own: if one file has an error while you are editing it, the error is printed in the log and
the OTHER tools (and the whole site) keep working. Only the broken tool's pages go missing (404) until you fix it."""
import importlib
import traceback

from routes.tools_common import tools_bp

TOOL_MODULES = ("routes.answer_tool", "routes.tracer_tool", "routes.topic_tool")
FAILED = []

for _m in TOOL_MODULES:
    try:
        importlib.import_module(_m)
    except Exception:                      # a bug in one tool must not take the others down
        FAILED.append(_m)
        print(f"[tools] {_m} failed to load. Its pages are off until it is fixed:")
        traceback.print_exc()

__all__ = ["tools_bp", "FAILED"]
