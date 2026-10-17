@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ============================================================
rem  立法雷达 LegisRadar（查看器） —— 零门槛启动器
rem
rem  [!] 本文件的编码纪律（不要改）：
rem      必须存成 GB18030 + CRLF，且不要加 chcp。
rem      原因：cmd.exe 按 OEM 代码页（中文系统 936）逐行读 .bat。
rem      存成 UTF-8 时，中文在 GBK 下会解出 & | 这类命令分隔符，
rem      rem 注释都拦不住 —— 整个脚本被撕成碎片执行，
rem      表现是「双击了什么都不发生」或满屏 not recognized。
rem
rem  这个程序零依赖，只要能跑 Python 3.11+ 就行，不需要装任何库。
rem  下面两段探测是为「连 Python 都没有」的机器准备的。
rem  没有可用的 Python 时，本脚本只告诉你怎么办 —— 不替你下载、不替你安装。
rem ============================================================

echo.
echo   立法雷达 LegisRadar
echo   看看有哪些法规草案正在征求意见
echo   --------------------------------------------------

set "PYEXE="
set "PYARGS="
set "PYOLD="
set "PYBROKEN="

rem ---------- (1) 系统 python ----------
where python >nul 2>nul
if %errorlevel%==0 (
    rem 先问一句「你跑得起来吗」—— where 找得到不等于能用。
    rem Windows 上的 python.exe 可能是应用商店的占位符：命令在，
    rem 跑起来却弹一个商店页面然后失败。把它说成「版本过低」是错的 ——
    rem 那台机器上压根没装过 Python，而「升级」和「安装」是两件事。
    python -c "" >nul 2>nul
    rem 判据是「不等于 0」。写成 if !errorlevel!==0 是反的 ——
    rem   cmd 把 !errorlevel!==0 解释成「errorlevel 等于 0」，
    rem   于是能用的 python 被判成坏的。
    if errorlevel 1 (
        set "PYBROKEN=1"
        echo   [环境] 系统 python 跑不起来（可能是应用商店的占位符）
    ) else (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set "PYEXE=python"
            echo   [环境] 使用系统 Python
            goto :checkdeps
        )
        rem 命令在、跑得起来、但版本不够。这和「完全没有 Python」是两回事：
        rem 那个要去装，这个要去升级，最后的说法不一样，所以先记下来。
        set "PYOLD=python"
        echo   [环境] 系统 Python 版本过低（需要 3.11 或更高）
    )
)

rem ---------- (2) py 启动器 ----------
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PYEXE=py"
        set "PYARGS=-3"
        echo   [环境] 使用 py 启动器
        goto :checkdeps
    )
    set "PYOLD=py"
    echo   [环境] py 启动器里没有 3.11 或更高的 Python
)

rem ---------- (3) 都没有：给指引，但「升级」和「安装」分开说 ----------
echo.
if defined PYBROKEN (
    echo   系统里的 python 跑不起来 —— 多半是应用商店的占位符，不是真的 Python。
    echo   下面这条安装指引对这种情况同样适用：
    echo.
    echo   到 python.org 装一个 Python 3.11 或更新版：
    echo       https://www.python.org/downloads/
    echo   安装时记得勾选 "Add Python to PATH"，装完再双击本文件。
) else if defined PYOLD (
    echo   这台电脑上有 Python，但版本低于 3.11。
    echo   要做的是升级 —— 不必先卸掉旧的，把新的装上去覆盖就行：
    echo       https://www.python.org/downloads/
    echo   装的时候勾选 "Add Python to PATH"，装完再双击本文件。
    echo.
    echo   为什么非要 3.11：本程序读配置用 tomllib，它到 3.11 才进标准库。
    echo   更早的版本上读不了 config.toml，程序只能靠默认值硬跑，
    echo   于是它认不出你已经配好的东西。
) else (
    echo   这台电脑上没找到 Python。
    echo.
    echo   到 python.org 装一个 Python 3.11 或更新版：
    echo       https://www.python.org/downloads/
    echo   安装时记得勾选 "Add Python to PATH"，装完再双击本文件。
)
echo.
echo   本脚本只做检测，不会替你下载或安装任何东西。
echo.
pause
exit /b 1

:checkdeps
rem ============================================================
rem  [!] 清掉 PYTHONUTF8 / PYTHONIOENCODING
rem      有些机器会设 PYTHONUTF8=1，Python 会无视控制台、
rem      强制输出 UTF-8。而 936 控制台按 GBK 解，于是 Python 的输出
rem      全是乱码 —— 而 .bat 自己的中文（GB18030）又显示正常，
rem      同一个窗口里两种编码打架。
rem      只在子进程里清掉，不动系统设置。
rem ============================================================
set "PYTHONUTF8="
set "PYTHONIOENCODING="

set "PYTHONPATH=%~dp0src"

echo   [执行] 拉取最新条目……
echo.
%PYEXE% %PYARGS% -m legisradar sync %*
set "RC=%errorlevel%"
echo.

rem 同步没成功也不停车。
rem
rem sync 自己已经分过档：离线但本地有数据时它返回 0，还顺手重出了一版看板。
rem 会返回非零的是另外几种（离线且本地空、没有索引地址、索引一条都用不了），
rem 也都不是「再试也没用」的错。停在这里才是真的糟：窗口一闪就没了，人以为
rem 工具坏了，而其实上一次的数据还好好地躺在本地，看板照出。
if not "%RC%"=="0" (
    echo   [注意] 这次没同步上（退出码 %RC%）。
    echo          多半是网络不通，或者索引地址一时打不开。
    echo          看板照样出，但上面的日期可能停在上一回。
    echo.
)

echo   [执行] 生成看板……
%PYEXE% %PYARGS% -m legisradar board
echo.

if exist "board.html" (
    echo   [执行] 起本地服务：页面上可手动标「已截止」……
    %PYEXE% %PYARGS% -m legisradar serve
    echo.
    echo   服务已停止（想再看板：重新双击本文件即可）。
) else (
    echo   看板未生成，请检查上方日志。
)
echo.
pause >nul
endlocal
