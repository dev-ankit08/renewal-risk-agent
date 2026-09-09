@echo off
start "" http://127.0.0.1:5000
"%~dp0.venv\Scripts\python.exe" "%~dp0webapp.py"
