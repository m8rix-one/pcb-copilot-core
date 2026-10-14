"""Where PCB Copilot Core keeps its files: projects, part libraries, datasheets and caches, in the user folder."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))          # the repository
HOME = os.path.expanduser(os.environ.get("PCB_COPILOT_HOME", "~/.pcb-copilot-core"))
PROJECTS = os.environ.get("PCB_COPILOT_PROJECTS", os.path.join(HOME, "projects"))
