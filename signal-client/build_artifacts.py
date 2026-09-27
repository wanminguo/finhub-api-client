#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_artifacts.py —— 打包客户端（只用标准库，不依赖 pip / build / setuptools）
================================================================================
为什么不用 `python -m build` / `pip wheel`：
  ① 构建机上 pip 的临时目录常被安全软件/沙箱挡住（本地实测 PermissionError）；
  ② 客户那边**根本不该需要 PyPI** —— 国内到不了，装不上的包等于没有。
所以这里直接手写 wheel（PEP 427 就是一个特定布局的 zip）和源码包：

  dist/finhub_signal_client-0.1.0-py3-none-any.whl     # 能给 pip 装（有 PyPI 的人）
  dist/finhub-signal-client-0.1.0.tar.gz               # 解压即用，**零安装**（推荐给国内用户）
  dist/sha256.txt                                      # 校验和（下载页一并给出）

用法：  python build_artifacts.py            # 在 client/ 目录下执行
"""

import base64
import hashlib
import os
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(HERE, "dist")
NAME = "finhub-signal-client"
PKG = "finhub"
VERSION = "0.1.0"
# ★ wheel 的 dist-info 目录名必须是**归一化后的发行名**（连字符 → 下划线），
#   而且要跟 wheel 文件名前缀一致 —— 写成 finhub-0.1.0.dist-info 的话
#   pip 会因为"名字对不上"直接拒绝安装。
NORM = NAME.replace("-", "_")
DIST_INFO = "%s-%s.dist-info" % (NORM, VERSION)
WHEEL_NAME = "%s-%s-py3-none-any.whl" % (NORM, VERSION)


def files():
    """要打进包里的文件（相对 client/ 的路径）。"""
    out = ["pyproject.toml", "README.md"]
    for f in sorted(os.listdir(os.path.join(HERE, PKG))):
        if f.endswith(".py"):
            out.append(os.path.join(PKG, f))
    return out


def read(p):
    with open(os.path.join(HERE, p), "rb") as fh:
        return fh.read()


METADATA = """Metadata-Version: 2.1
Name: {name}
Version: {ver}
Summary: FinHub 信号客户端：订阅 BTC 信号、本地下单、回执回传（纯标准库，实盘可选 py-clob-client）
Home-page: https://api.wanminguo.top/
License: Proprietary
Requires-Python: >=3.8
Description-Content-Type: text/markdown

{readme}
"""

WHEEL = """Wheel-Version: 1.0
Generator: finhub-build_artifacts
Root-Is-Purelib: true
Tag: py3-none-any
"""

ENTRY = """[console_scripts]
finhub = finhub.finhub:main
"""


def build_wheel():
    import io
    os.makedirs(DIST, exist_ok=True)
    path = os.path.join(DIST, WHEEL_NAME)
    records = []            # (arcname, sha256_b64, size)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        def add(arcname, data):
            z.writestr(arcname, data)
            h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            records.append((arcname, "sha256=" + h, str(len(data))))

        for f in files():
            add(f.replace(os.sep, "/"), read(f))
        add("%s/METADATA" % DIST_INFO,
            METADATA.format(name=NAME, ver=VERSION,
                            readme=read("README.md").decode("utf-8")).encode("utf-8"))
        add("%s/WHEEL" % DIST_INFO, WHEEL.encode("utf-8"))
        add("%s/entry_points.txt" % DIST_INFO, ENTRY.encode("utf-8"))
        # RECORD 自己那行留空哈希（PEP 427 允许）
        lines = ["%s,%s,%s" % (a, h, s) for a, h, s in records]
        lines.append("%s/RECORD,," % DIST_INFO)
        z.writestr("%s/RECORD" % DIST_INFO, ("\n".join(lines) + "\n").encode("utf-8"))
    return path


def build_sdist():
    os.makedirs(DIST, exist_ok=True)
    path = os.path.join(DIST, "%s-%s.tar.gz" % (NAME, VERSION))
    root = "%s-%s" % (NAME, VERSION)
    with tarfile.open(path, "w:gz") as t:
        for f in files():
            arc = os.path.join(root, f).replace(os.sep, "/")
            info = t.gettarinfo(os.path.join(HERE, f), arcname=arc)
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = 0o644
            with open(os.path.join(HERE, f), "rb") as fh:
                t.addfile(info, fh)
    return path


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    built = [build_wheel(), build_sdist()]
    lines = []
    for p in built:
        d = sha256(p)
        lines.append("%s  %s" % (d, os.path.basename(p)))
        print("built %-52s %8d bytes  sha256=%s…"
              % (os.path.basename(p), os.path.getsize(p), d[:16]))
    with open(os.path.join(DIST, "sha256.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n校验和写入 dist/sha256.txt")
    print("发布：把 dist/ 里的文件放到平台 webroot 的 download/ 目录即可。")


if __name__ == "__main__":
    main()
