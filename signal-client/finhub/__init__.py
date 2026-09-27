"""FinHub 信号客户端（纯标准库；实盘下单可选依赖 py-clob-client）。

    from finhub.finhub import main
    main(["--key", "...", "--paper"])
"""
from .finhub import main, VERSION          # noqa: F401

__all__ = ["main", "VERSION"]
