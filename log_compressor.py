#!/usr/bin/env python3
"""日志压缩与关键信息提取系统 — 向后兼容包装脚本。

实际实现位于 log_compressor/ 模块。
用法与之前一致，推荐使用 python -m log_compressor。
"""

from log_compressor.__main__ import main

if __name__ == "__main__":
    main()
