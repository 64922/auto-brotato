"""pytest 入口辅助：确保从任意工作目录运行都能导入 ab_agent 包。"""
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
