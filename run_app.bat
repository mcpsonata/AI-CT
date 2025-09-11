@echo off
echo Starting Power BI MCP Application...

:: Activate the virtual environment if it exists
if exist .venv\Scripts\activate.bat (
    echo Activating virtual environment...
    call .venv\Scripts\activate.bat
) else (
    echo Virtual environment not found, using system Python...
)

:: Run the application
echo Running the application...
python webapp\app.py

:: Keep the window open if there's an error
if %ERRORLEVEL% neq 0 (
    echo An error occurred while running the application.
    echo Error code: %ERRORLEVEL%
    pause
)
