"""连接 Neon。这一层只管「怎么连上」，不管「查什么」。

两条连接串，用途分开，不是二选一：

    pooled   业务查询走这条。连接从 PgBouncer 的池子里借，短连接、
             并发多、跑完就断的场景不用每次重新握手。
    direct   migration 和 LangGraph 的 checkpointer 走这条。

为什么 migration 不能走 pooled：PgBouncer 是 transaction 模式，
一条连接在两个事务之间会被换给别人，凡是「跨语句还得记得上一句干了
什么」的东西都不成立 —— 建表时的锁、CREATE INDEX CONCURRENTLY、
会话级设置都属于这类。checkpointer 另有理由：它连接数少又常驻，
本来就用不上池子。

（实测过一件事，别照抄旧结论：驱动做的协议级 prepared statement
在 Neon 的 pooler 下是好的，psycopg3 自动 prepare 也没问题。
不支持的是 SQL 级的 PREPARE / EXECUTE。）
"""

from __future__ import annotations

import contextlib
import os
import pathlib
from collections.abc import Iterator

import psycopg

ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"

POOLED_VAR = "DATABASE_URL"
DIRECT_VAR = "DATABASE_URL_DIRECT"


def _load_env_file() -> dict[str, str]:
    """把 .env 读成一个字典。文件不在就返回空的。

    输入：无，固定读仓库根目录的 .env
    输出：键到值的字典

    自己解析而不是用 python-dotenv，一是少一个依赖，二是绕开那个坑：
    load_dotenv() 要是比模块级的 os.getenv() 晚执行，读到的是默认值
    而不是文件里的值，而这种错静默得很。这里干脆不做模块级读取，
    每次要用的时候现读现取。
    """
    if not ENV_PATH.exists():
        return {}

    out: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def connection_url(*, direct: bool = False) -> str:
    """取一条连接串。

    输入：direct=True 要不走池子的那条，默认要 pooled 那条
    输出：连接串

    先看真正的环境变量，再回落到 .env 文件。顺序是这样定的
    因为上 EC2 之后密钥由 systemd 的 Environment= 给，那时候不该有
    .env 文件，也不该因为没有它就连不上。

    两条都没有就炸，不给默认值也不静默连本地 —— 「以为连的是 Neon，
    其实连了别处」是最难查的一类错。
    """
    var = DIRECT_VAR if direct else POOLED_VAR
    url = os.environ.get(var) or _load_env_file().get(var)
    if not url:
        raise RuntimeError(
            f"没有 {var}。环境变量和 {ENV_PATH.name} 里都没找到。"
            f"Neon 控制台 Connect 面板，Connection pooling "
            f"{'关' if direct else '开'}的那条。"
        )
    return url


@contextlib.contextmanager
def connect(*, direct: bool = False, autocommit: bool = False) -> Iterator[psycopg.Connection]:
    """连上库，用完自动关。

    输入：direct 选哪条串；autocommit 要不要关掉事务包裹
    输出：一个连接，退出时提交（出异常则回滚）并关闭

    autocommit 留出来是给少数不能待在事务里的语句用的
    （CREATE INDEX CONCURRENTLY 是典型），平时别开 ——
    默认包在事务里，中途炸了整批回滚，比留下半张表干净。
    """
    conn = psycopg.connect(connection_url(direct=direct), autocommit=autocommit)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()
