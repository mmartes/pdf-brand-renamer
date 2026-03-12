@echo off
REM ============================================================
REM  Build PDF Brand Renamer into a standalone Windows .exe
REM ============================================================
REM  Prerequisites:
REM    1. Python 3.10+ installed and on PATH
REM    2. Run:  pip install -r requirements.txt
REM ============================================================

echo.
echo  Installing dependencies...
pip install -r requirements.txt
echo.
echo  Building executable...
pyinstaller --onefile --windowed --name "PDF Brand Renamer" pdf_brand_renamer.py

echo.
echo  ============================================================
echo   Done!  Your executable is in the "dist" folder:
echo   dist\PDF Brand Renamer.exe
echo  ============================================================
pause
