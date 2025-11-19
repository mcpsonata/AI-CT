@echo off
echo Activating Python virtual environment...
call .venv\Scripts\activate

echo Starting the application...
cd webapp
python app.py 2>&1


