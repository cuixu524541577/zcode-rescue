@echo off
cd /d "%~dp0"
chcp 65001 >nul
where pythonw >nul 2>nul && (start "" pythonw "zcode_rescue.py" & exit /b 0)
where pyw >nul 2>nul && (start "" pyw -3 "zcode_rescue.py" & exit /b 0)
where python >nul 2>nul && (python "zcode_rescue.py" & exit /b 0)
where py >nul 2>nul && (py -3 "zcode_rescue.py" & exit /b 0)
echo.
echo 未找到 Python。请先安装 Python 3.9 或更高版本：
echo     https://www.python.org/downloads/
echo 安装时请勾选 "Add python.exe to PATH"，装好后再双击本文件。
echo.
pause
