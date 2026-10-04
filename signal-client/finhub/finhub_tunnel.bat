@echo off
chcp 65001 >nul
rem ============================================================
rem  FinHub 信号端 · 公网远程查看隧道（SSH 反向隧道）
rem  作用：把本机 8787 面板映射到 https://api.wanminguo.top/local/
rem        —— 手机/任意设备打开该网址即可查看（同平台账号登录）。
rem  依赖：本机 OpenSSH Client（Win10/11 自带，无需安装 Python）。
rem  用法：
rem    · 双击本文件运行，窗口保持打开 = 隧道在线；关窗口 = 隧道断开。
rem    · 想一直在线：把本文件加入「启动」文件夹开机自启
rem      （Win+R 输入 shell:startup，把本文件快捷方式放进去）。
rem    · 首次提示 host key 时输入 yes 回车。
rem ============================================================
setlocal
set "KEY=D:\api.wanminguo.top\.deploy\id_ed25519"
set "SRV=root@43.161.239.203"
set "RPORT=8789"
set "LPORT=8787"

if not exist "%KEY%" (
  echo [错误] 找不到密钥：%KEY%
  echo 密钥不在客户端目录内（安全考虑），请把部署密钥路径改成你本机的实际路径后重试。
  pause
  exit /b 1
)

where ssh >nul 2>nul
if errorlevel 1 (
  echo [错误] 本机没有 ssh 命令。Win10/11 请到「设置-应用-可选功能」安装 OpenSSH 客户端。
  pause
  exit /b 1
)

echo 正在建立公网隧道 ...
echo   本机面板   http://127.0.0.1:%LPORT%/
echo   公网访问   https://api.wanminguo.top/local/
echo   手机/任意设备打开上面公网网址（同平台账号登录后即可查看/操作）
echo   窗口保持打开 = 隧道在线；按 Ctrl+C 或关窗口 = 断开
echo.
ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -i "%KEY%" -R %RPORT%:127.0.0.1:%LPORT% -p 22 %SRV%
echo.
echo 隧道已断开（请保持窗口打开才能远程查看）
pause
