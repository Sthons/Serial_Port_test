"""桌面程序入口：三串口通讯线路板测试工具。"""

from __future__ import annotations

import tkinter as tk

from app.gui import SerialTestApp


def main() -> None:
    root = tk.Tk()
    SerialTestApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
