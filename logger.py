"""Simple coloured console logger shared across all """
import sys
from datetime import datetime

ICONS = {
    "info":    "ℹ️ ",
    "success": "✅",
    "warning": "⚠️ ",
    "error":   "❌",
    "agent":   "🤖",
    "browser": "🌐",
    "vision":  "🔍",
    "llm":     "📝",
    "excel":   "📊",
    "run":     "▶️ ",
}

def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log(level: str, agent: str, message: str):
    icon = ICONS.get(level, "  ")
    print(f"[{_ts()}] {icon}  [{agent}] {message}", flush=True)

def section(title: str):
    bar = "─" * 60
    print(f"\n{bar}\n  {title}\n{bar}", flush=True)
