@echo off
rem Запуск GUI без окна консоли. Передаёт необязательный аргумент-папку.
start "" pythonw "%~dp0app.py" %*
