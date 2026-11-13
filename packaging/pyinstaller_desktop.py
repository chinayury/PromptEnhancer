"""PyInstaller 入口：桌面 GUI（绝对导入，避免包内 __main__ 的相对导入问题）。"""
from promptforge.__main__ import main

if __name__ == "__main__":
    main()
