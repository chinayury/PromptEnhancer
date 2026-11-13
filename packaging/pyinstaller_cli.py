"""PyInstaller 入口：promptforge 命令行模式。"""
import sys

from promptforge.cli import run

if __name__ == "__main__":
    sys.exit(run())
