# HireFlow AI ATS

HireFlow AI ATS is a recruitment and applicant-tracking platform for candidates, recruiters, and administrators. It supports job publishing, private CV handling, applications, application history, interviews, recruiter workflows, and responsible AI-assisted CV summaries.

## Features

- Candidate signup, email confirmation, and login
- Candidate CV upload and replacement with private Supabase Storage
- PDF CV validation with a 2 MB maximum
- Browse open jobs and submit applications
- CV snapshots preserved with applications
- Candidate application history, withdrawal, and re-apply support
- Admin job creation, opening, closing, recruiter management, and application decisions
- Recruiter job assignment, application review, private notes, stages, and interviews
- n8n webhook automation for application, status, interview, and AI-summary workflows
- Supabase Row Level Security and backend authorization checks

## Tech stack

- Frontend: HTML, CSS, vanilla JavaScript
- Backend: FastAPI, Python, Uvicorn
- Database, Auth, and Storage: Supabase
- Automation: n8n
- Deployment: Railway backend and hosted static frontend

## Project structure

```text
backend/
  main.py
  requirements.txt
  Dockerfile
  Procfile
  n8n_application_email.workflow.json
frontend/
  index.html
  app.js
  styles.css
```

## Backend setup

```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
python -m uvicorn main:app --reload
```

The API will be available at `http://127.0.0.1:8000` and Swagger at `/docs`.

## Environment variables

Copy `.env.example` to `.env` and fill in the local values for Supabase, n8n, callback security, and allowed frontend origins. The `.env` file is intentionally ignored by Git.

Never place service-role keys, callback secrets, passwords, API keys, or private Storage credentials in frontend code or GitHub.

## Frontend setup

The frontend is a static application. Serve the `frontend` folder with any static web server, or use the backend's `/portal` route during local development. The frontend API base is configured in `frontend/app.js` and points to the deployed Railway API by default.

## Railway deployment

Connect the repository to Railway with the service root directory set to `backend`. Railway can use `backend/Dockerfile`, or the start command:

```text
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Add the variables from `.env.example` in Railway Variables. Do not commit the real values. Configure the Railway public URL in `FRONTEND_ORIGINS` and configure the Supabase Auth Site URL and redirect URLs for the live frontend.

## Security note

Keep `.env`, Supabase service-role keys, n8n callback secrets, passwords, API keys, private CV paths, and signed URLs private. The `cvs` Storage bucket must remain private.

