@echo off
rem ============================================================
rem  FinHub 信号客户端 · 看门狗（开机自启 + 崩溃自动拉起）
rem  每 30 秒检查一次 8787 是否 LISTENING；不在监听就拉起 finhub.py
rem  ★ 必须同时匹配 :8787 + LISTENING —— 否则浏览器残留的 TIME_WAIT
rem    连接行会误判"已在监听"，导致崩溃后不拉起（2026-10-04 教训）
rem  ★ 2026-10-05（退出不了修复）：面板点『退出程序』会写
rem    %USERPROFILE%\.finhub\quit.flag —— 本脚本检测到该标记就
rem    删除标记并退出循环（不再自动拉起），实现"真正退出"。
rem ============================================================
setlocal
cd /d "D:\api.wanminguo.top\client\finhub"
:loop
if exist "%USERPROFILE%\.finhub\quit.flag" (
  del "%USERPROFILE%\.finhub\quit.flag" >nul 2>nul
  echo [watchdog] 收到退出标记，停止守护。退出后如需再用源码模式，
  echo [watchdog] 请重新运行本脚本（finhub_watchdog.bat）。
  exit /b 0
)
netstat -ano | findstr /c:":8787" | findstr /c:"LISTENING" >nul 2>nul
if errorlevel 1 (
  C:\Python314\python.exe finhub.py >> run.log 2>> run.err.log
)
timeout /t 30 /nobreak >nul
goto loop
