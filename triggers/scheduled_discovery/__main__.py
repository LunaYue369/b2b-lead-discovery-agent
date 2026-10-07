"""python -m triggers.scheduled_discovery 走的是这个文件。

python -m 后面跟一个包（文件夹）时，Python 执行的是包里的 __main__.py。
这里只转一手，入口本体在 main.py：

    输入   命令行参数，如 python -m triggers.scheduled_discovery --no-slack
    输出   进程退出码。main() 返回 0 或 1，sys.exit 把它交给 systemd
"""

import sys

from triggers.scheduled_discovery.main import main

sys.exit(main())
