import os
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from supabase import create_client

load_dotenv()

url = (os.getenv("SUPABASE_URL") or "").strip().strip('"').strip("'").rstrip("/")
if url.endswith("/rest/v1"):
    url = url[:-len("/rest/v1")]
key = (os.getenv("SUPABASE_KEY") or "").strip().strip('"').strip("'")
service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
n8n_application_webhook_url = os.getenv("N8N_APPLICATION_WEBHOOK_URL")
n8n_ai_webhook_url = os.getenv("N8N_AI_WEBHOOK_URL")
n8n_interview_webhook_url = os.getenv("N8N_INTERVIEW_WEBHOOK_URL")
n8n_status_webhook_url = os.getenv("N8N_STATUS_WEBHOOK_URL")
n8n_callback_secret = os.getenv("N8N_CALLBACK_SECRET")

if not url or not key:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY are required in .env")

app = FastAPI(title="Recruitment Portal API")
supabase = create_client(url, key)

frontend_origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/portal", StaticFiles(directory=str(frontend_dir), html=True), name="portal")


def create_admin_client():
    if not service_role_key:
        raise HTTPException(
            status_code=503,
            detail="Recruiter invite service is not configured on the backend",
        )
    return create_client(url, service_role_key)


def get_bearer_token(authorization):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")
    return token


def require_active_recruiter(authorization):
    token = get_bearer_token(authorization)
    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    recruiter_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)
    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role,is_active")
            .eq("id", recruiter_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Recruiter profile required")

    if not profile_result.data or profile_result.data[0].get("role") != "recruiter":
        raise HTTPException(status_code=403, detail="Only recruiters can use this endpoint")
    if not profile_result.data[0].get("is_active", True):
        raise HTTPException(status_code=403, detail="Recruiter account is inactive")
    return token, recruiter_id, user_supabase


def require_admin_user(authorization):
    token = get_bearer_token(authorization)
    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    admin_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)
    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role,is_active")
            .eq("id", admin_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Admin profile required")

    if not profile_result.data or profile_result.data[0].get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can use this endpoint")
    if not profile_result.data[0].get("is_active", True):
        raise HTTPException(status_code=403, detail="Admin account is inactive")
    return token, admin_id, user_supabase


def require_assigned_application(user_supabase, recruiter_id, application_id):
    try:
        application_result = (
            user_supabase.table("applications")
            .select("id,job_id,candidate_id,resume_path,status,applied_at")
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load application: {e}")

    if not application_result.data:
        raise HTTPException(status_code=404, detail="Application not found")
    application = application_result.data[0]

    try:
        assignment_result = (
            user_supabase.table("recruiter_jobs")
            .select("job_id")
            .eq("recruiter_id", recruiter_id)
            .eq("job_id", application["job_id"])
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not verify job assignment: {e}")

    if not assignment_result.data:
        raise HTTPException(status_code=403, detail="Recruiter is not assigned to this application")
    return application


async def send_n8n_webhook(webhook_url, payload):
    if not webhook_url:
        return False
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(webhook_url, json=payload)
            return 200 <= response.status_code < 300
    except Exception:
        return False


def get_automation_context(application_id, job_id):
    context = {
        "application_id": application_id,
        "job_id": job_id,
        "candidate_name": None,
        "candidate_email": None,
        "job_title": None,
        "job_requirements": None,
    }
    if not service_role_key:
        return context
    try:
        admin_supabase = create_admin_client()
        application_result = (
            admin_supabase.table("applications")
            .select("candidate_id")
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
        if application_result.data:
            candidate_result = (
                admin_supabase.table("profiles")
                .select("full_name,email")
                .eq("id", application_result.data[0]["candidate_id"])
                .limit(1)
                .execute()
            )
            if candidate_result.data:
                context["candidate_name"] = candidate_result.data[0].get("full_name")
                context["candidate_email"] = candidate_result.data[0].get("email")
        job_result = (
            admin_supabase.table("jobs")
            .select("title,requirements")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
        if job_result.data:
            context["job_title"] = job_result.data[0].get("title")
            context["job_requirements"] = job_result.data[0].get("requirements")
    except Exception:
        pass
    return context


def create_storage_signed_url(storage_path, expires_in=300):
    if not storage_path or not service_role_key:
        return None
    try:
        admin_supabase = create_admin_client()
        signed_result = admin_supabase.storage.from_("cvs").create_signed_url(
            storage_path,
            expires_in,
        )
        return signed_result.get("signedURL") or signed_result.get("signedUrl")
    except Exception:
        return None


@app.get("/")
async def home():
    return {"message": "Recruitment Portal API is running"}


@app.post("/signup")
async def signup(request: Request):
    data = await request.json()

    try:
        response = supabase.auth.sign_up(
            {
                "email": data["email"],
                "password": data["password"],
                "options": {
                    "data": {
                        "full_name": data["full_name"],
                        "phone": data["phone"],
                        "role": "candidate",
                    }
                },
            }
        )

        if not response.user:
            return {"error": "Signup failed: user was not created"}

        user_id = response.user.id

        return {
            "message": "Candidate registered successfully",
            "user_id": user_id,
            "profile_created": True,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/login")
async def login(request: Request):
    data = await request.json()

    try:
        response = supabase.auth.sign_in_with_password(
            {
                "email": data["email"],
                "password": data["password"],
            }
        )
        login_supabase = create_client(url, key)
        login_supabase.postgrest.auth(response.session.access_token)
        profile_result = (
            login_supabase.table("profiles")
            .select("role")
            .eq("id", response.user.id)
            .limit(1)
            .execute()
        )
        role = profile_result.data[0].get("role") if profile_result.data else None

        return {
            "message": "Login successful",
            "access_token": response.session.access_token,
            "user_id": response.user.id,
            "role": role,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/jobs")
async def create_job(request: Request, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        verified_user = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not verified_user.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        user_response = supabase.auth.get_user(token)
        if not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid token")

        user_id = user_response.user.id

        # Use the caller's JWT for the database request so RLS sees auth.uid().
        user_supabase = create_client(url, key)
        user_supabase.postgrest.auth(token)

        profile = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .single()
            .execute()
        )

        if not profile.data or profile.data["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only admin can create jobs")

        data = await request.json()
        required_fields = [
            "title",
            "company_name",
            "department",
            "description",
            "requirements",
            "location",
            "employment_type",
            "last_date",
            "openings",
        ]
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            raise HTTPException(
                status_code=422,
                detail=f"Missing fields: {', '.join(missing_fields)}",
            )

        job = {
            "created_by": user_id,
            "title": data["title"],
            "company_name": data["company_name"],
            "department": data["department"],
            "description": data["description"],
            "requirements": data["requirements"],
            "location": data["location"],
            "employment_type": data["employment_type"],
            "salary_min": data.get("salary_min"),
            "salary_max": data.get("salary_max"),
            "last_date": data["last_date"],
            "openings": data["openings"],
            "status": "draft",
        }

        result = user_supabase.table("jobs").insert(job).execute()
        return {"message": "Job created successfully", "job": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/jobs/{job_id}/open")
async def open_job(job_id: str, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        verified_user = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not verified_user.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        user_response = supabase.auth.get_user(token)
        if not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid token")

        user_id = user_response.user.id
        user_supabase = create_client(url, key)
        user_supabase.postgrest.auth(token)

        profile = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .single()
            .execute()
        )

        if not profile.data or profile.data["role"] != "admin":
            raise HTTPException(status_code=403, detail="Only admin can open jobs")

        result = (
            user_supabase.table("jobs")
            .update({"status": "open"})
            .eq("id", job_id)
            .eq("status", "draft")
            .execute()
        )

        if not result.data:
            raise HTTPException(
                status_code=400,
                detail="Job not found or job is not draft",
            )

        return {"message": "Job opened successfully", "job": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/jobs/{job_id}/close")
async def close_job(job_id: str, authorization: str = Header(None)):
    token = get_bearer_token(authorization)
    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)
    try:
        profile = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_response.user.id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not verify admin profile: {e}")
    if not profile.data or profile.data[0].get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admin can close jobs")

    try:
        result = (
            user_supabase.table("jobs")
            .update({"status": "closed"})
            .eq("id", job_id)
            .in_("status", ["draft", "open"])
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not close job: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Job not found or already final")
    return {"message": "Job closed successfully", "job": result.data[0]}


@app.get("/admin/dashboard")
async def admin_dashboard(authorization: str = Header(None)):
    token = get_bearer_token(authorization)
    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)
    try:
        profile = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_response.user.id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not verify admin profile: {e}")
    if not profile.data or profile.data[0].get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admin can view dashboard")

    try:
        jobs_result = user_supabase.table("jobs").select(
            "id,title,company_name,department,location,employment_type,last_date,openings,status,created_at"
        ).order("created_at", desc=True).execute()
        recruiters_result = user_supabase.table("profiles").select(
            "id,full_name,email,phone,role,is_active,created_at"
        ).eq("role", "recruiter").order("created_at", desc=True).execute()
        assignments_result = user_supabase.table("recruiter_jobs").select(
            "id,recruiter_id,job_id,assigned_at"
        ).execute()
        applications_result = user_supabase.table("applications").select(
            "id,job_id,status,applied_at"
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load dashboard data: {e}")

    pipeline_statuses = ["applied", "shortlisted", "interview", "offer", "hired", "rejected", "withdrawn"]
    counts_by_job = {}
    total_counts = {status: 0 for status in pipeline_statuses}
    for application in applications_result.data or []:
        status = application.get("status")
        if status not in total_counts:
            continue
        total_counts[status] += 1
        counts_by_job.setdefault(application.get("job_id"), {key: 0 for key in pipeline_statuses})
        counts_by_job[application.get("job_id")][status] += 1

    return {
        "jobs": [
            {**job, "pipeline_counts": counts_by_job.get(job["id"], {key: 0 for key in pipeline_statuses})}
            for job in (jobs_result.data or [])
        ],
        "recruiters": recruiters_result.data or [],
        "assignments": assignments_result.data or [],
        "pipeline_counts": total_counts,
        "application_count": len(applications_result.data or []),
    }


@app.get("/admin/applications")
async def list_admin_applications(authorization: str = Header(None)):
    _, _, _ = require_admin_user(authorization)
    try:
        admin_supabase = create_admin_client()
        applications_result = (
            admin_supabase.table("applications")
            .select("id,job_id,candidate_id,status,applied_at,resume_path")
            .order("applied_at", desc=True)
            .execute()
        )
        applications = applications_result.data or []
        job_ids = list({item["job_id"] for item in applications})
        candidate_ids = list({item["candidate_id"] for item in applications})
        jobs_result = (
            admin_supabase.table("jobs")
            .select("id,title,company_name,department")
            .in_("id", job_ids)
            .execute()
            if job_ids
            else None
        )
        profiles_result = (
            admin_supabase.table("profiles")
            .select("id,full_name,email")
            .in_("id", candidate_ids)
            .execute()
            if candidate_ids
            else None
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load applications: {e}")

    jobs_by_id = {item["id"]: item for item in (jobs_result.data if jobs_result else [])}
    candidates_by_id = {item["id"]: item for item in (profiles_result.data if profiles_result else [])}
    return {
        "applications": [
            {
                "id": item["id"],
                "status": item["status"],
                "applied_at": item["applied_at"],
                "has_cv": bool(item.get("resume_path")),
                "candidate": candidates_by_id.get(item["candidate_id"], {}),
                "job": jobs_by_id.get(item["job_id"], {"id": item["job_id"]}),
            }
            for item in applications
        ]
    }


@app.put("/admin/applications/{application_id}/decision")
async def decide_admin_application(
    application_id: str,
    request: Request,
    authorization: str = Header(None),
):
    _, _, _ = require_admin_user(authorization)
    data = await request.json()
    decision = str(data.get("status", "")).strip().lower()
    if decision not in {"shortlisted", "rejected"}:
        raise HTTPException(status_code=422, detail="status must be shortlisted or rejected")

    try:
        admin_supabase = create_admin_client()
        application_result = (
            admin_supabase.table("applications")
            .select("id,job_id,candidate_id,status")
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
        if not application_result.data:
            raise HTTPException(status_code=404, detail="Application not found")
        application = application_result.data[0]
        if application["status"] != "applied":
            raise HTTPException(status_code=409, detail="Only applied applications can be confirmed or rejected")
        updated = (
            admin_supabase.table("applications")
            .update({"status": decision})
            .eq("id", application_id)
            .eq("status", "applied")
            .execute()
        )
        if not updated.data:
            raise HTTPException(status_code=409, detail="Application stage changed; retry the request")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not update application: {e}")

    automation_context = get_automation_context(application_id, application["job_id"])
    await send_n8n_webhook(
        n8n_status_webhook_url,
        {
            "event": f"application_{decision}",
            "application_id": application_id,
            "job_id": application["job_id"],
            "candidate_id": application["candidate_id"],
            "candidate_name": automation_context["candidate_name"],
            "candidate_email": automation_context["candidate_email"],
            "job_title": automation_context["job_title"],
            "status": decision,
        },
    )
    return {"message": f"Application {decision} successfully", "application": updated.data[0]}


@app.get("/jobs")
async def list_available_jobs(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        verified_user = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not verified_user.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        user_response = supabase.auth.get_user(token)
        if not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid token")

        user_supabase = create_client(url, key)
        user_supabase.postgrest.auth(token)

        fields = (
            "id,title,company_name,department,description,requirements,"
            "location,employment_type,salary_min,salary_max,last_date,"
            "openings,status,created_at"
        )
        result = (
            user_supabase.table("jobs")
            .select(fields)
            .eq("status", "open")
            .gte("last_date", date.today().isoformat())
            .order("created_at", desc=True)
            .execute()
        )

        return {"jobs": result.data or []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/jobs/{job_id}")
async def get_available_job(job_id: str, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        verified_user = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if not verified_user.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    try:
        user_response = supabase.auth.get_user(token)
        if not user_response.user:
            raise HTTPException(status_code=401, detail="Invalid token")

        user_supabase = create_client(url, key)
        user_supabase.postgrest.auth(token)

        fields = (
            "id,title,company_name,department,description,requirements,"
            "location,employment_type,salary_min,salary_max,last_date,"
            "openings,status,created_at"
        )
        result = (
            user_supabase.table("jobs")
            .select(fields)
            .eq("id", job_id)
            .eq("status", "open")
            .gte("last_date", date.today().isoformat())
            .limit(1)
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Job not available")

        return {"job": result.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/candidate/cv")
async def upload_candidate_cv(
    file: UploadFile = File(None),
    authorization: str = Header(None),
):
    max_cv_size = 2 * 1024 * 1024

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)
    user_supabase.options.headers["Authorization"] = f"Bearer {token}"

    try:
        profile = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .single()
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Candidate profile required")

    if not profile.data or profile.data["role"] != "candidate":
        raise HTTPException(status_code=403, detail="Only candidates can upload CVs")

    if not file:
        raise HTTPException(status_code=400, detail="CV file is required")

    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")

    file_name = file.filename or ""
    if not file_name.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="CV filename must end with .pdf")

    file_bytes = await file.read(max_cv_size + 1)
    if not file_bytes:
        raise HTTPException(status_code=400, detail="CV file cannot be empty")
    if len(file_bytes) > max_cv_size:
        raise HTTPException(status_code=400, detail="CV file must be 2 MB or smaller")

    storage_path = f"{user_id}/current_cv.pdf"

    try:
        user_supabase.storage.from_("cvs").upload(
            storage_path,
            file_bytes,
            {
                "upsert": "true",
                "content-type": "application/pdf",
            },
        )

        profile_update = (
            user_supabase.table("profiles")
            .update({"cv_path": storage_path})
            .eq("id", user_id)
            .execute()
        )
        if not profile_update.data:
            raise HTTPException(status_code=400, detail="Could not save CV path")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CV upload failed: {e}")

    return {"message": "CV uploaded successfully", "file_name": "current_cv.pdf"}


@app.post("/jobs/{job_id}/apply")
async def apply_to_job(job_id: str, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)
    user_supabase.options.headers["Authorization"] = f"Bearer {token}"

    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role,cv_path,full_name,email")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Candidate profile required")

    if not profile_result.data:
        raise HTTPException(status_code=403, detail="Candidate profile required")

    profile = profile_result.data[0]
    if profile["role"] != "candidate":
        raise HTTPException(status_code=403, detail="Only candidates can apply")

    try:
        job_result = (
            user_supabase.table("jobs")
            .select("id,status,last_date,openings,title,requirements")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Job not found")

    if not job_result.data:
        raise HTTPException(status_code=404, detail="Job not found")

    job = job_result.data[0]
    if job["status"] != "open":
        raise HTTPException(status_code=400, detail="Job is not open")
    if not job["last_date"] or job["last_date"] < date.today().isoformat():
        raise HTTPException(status_code=400, detail="Job application deadline has passed")
    if job["openings"] is None or job["openings"] <= 0:
        raise HTTPException(status_code=400, detail="No openings are available")

    current_cv_path = profile.get("cv_path")
    expected_current_cv_path = f"{user_id}/current_cv.pdf"
    if current_cv_path != expected_current_cv_path:
        raise HTTPException(status_code=400, detail="Please upload your CV before applying")

    try:
        current_cv_exists = user_supabase.storage.from_("cvs").exists(current_cv_path)
    except Exception:
        current_cv_exists = False
    if not current_cv_exists:
        raise HTTPException(status_code=400, detail="Please upload your CV before applying")

    active_statuses = ["applied", "shortlisted", "interview"]
    try:
        duplicate_result = (
            user_supabase.table("applications")
            .select("id,status")
            .eq("job_id", job_id)
            .eq("candidate_id", user_id)
            .in_("status", active_statuses)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not check existing applications: {e}")

    if duplicate_result.data:
        raise HTTPException(
            status_code=409,
            detail="You already have an active application for this job",
        )

    application_id = str(uuid.uuid4())
    snapshot_path = f"{user_id}/applications/{application_id}/cv.pdf"

    try:
        user_supabase.storage.from_("cvs").copy(current_cv_path, snapshot_path)

        application = {
            "id": application_id,
            "job_id": job_id,
            "candidate_id": user_id,
            "resume_path": snapshot_path,
            "status": "applied",
        }
        application_result = (
            user_supabase.table("applications").insert(application).execute()
        )
        if not application_result.data:
            raise RuntimeError("Application was not created")
    except Exception as e:
        try:
            user_supabase.storage.from_("cvs").remove([snapshot_path])
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"Application submission failed: {e}")

    cv_signed_url = create_storage_signed_url(snapshot_path)
    await send_n8n_webhook(
        n8n_application_webhook_url,
        {
            "event": "application_created",
            "application_id": application_id,
            "job_id": job_id,
            "candidate_id": user_id,
            "candidate_name": profile.get("full_name"),
            "candidate_email": profile.get("email"),
            "job_title": job.get("title"),
            "job_requirements": job.get("requirements"),
            "resume_path": snapshot_path,
            "cv_signed_url": cv_signed_url,
            "cv_url_expires_in": 300,
        },
    )

    return {
        "message": "Application submitted successfully",
        "application": {
            "id": application_id,
            "job_id": job_id,
            "status": "applied",
        },
    }


@app.get("/my-applications")
async def get_my_applications(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Candidate profile required")

    if not profile_result.data:
        raise HTTPException(status_code=403, detail="Candidate profile required")
    if profile_result.data[0]["role"] != "candidate":
        raise HTTPException(status_code=403, detail="Only candidates can view applications")

    try:
        applications_result = (
            user_supabase.table("applications")
            .select("id,job_id,status,applied_at")
            .eq("candidate_id", user_id)
            .order("applied_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load applications: {e}")

    applications = applications_result.data or []
    if not applications:
        return {"applications": []}

    application_ids = [application["id"] for application in applications]
    job_ids = list({application["job_id"] for application in applications})

    try:
        jobs_result = (
            user_supabase.table("jobs")
            .select("id,title,company_name,department,location,employment_type")
            .in_("id", job_ids)
            .execute()
        )
        interviews_result = (
            user_supabase.table("interviews")
            .select("id,application_id,scheduled_at,mode,meeting_url,status")
            .in_("application_id", application_ids)
            .order("scheduled_at", desc=False)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load application details: {e}")

    jobs_by_id = {job["id"]: job for job in (jobs_result.data or [])}
    interview_by_application = {}
    for interview in interviews_result.data or []:
        interview_by_application.setdefault(interview["application_id"], interview)

    response_applications = []
    for application in applications:
        job = jobs_by_id.get(application["job_id"])
        interview = interview_by_application.get(application["id"])

        safe_interview = None
        if interview:
            safe_interview = {
                "scheduled_at": interview["scheduled_at"],
                "mode": interview["mode"],
                "meeting_url": interview["meeting_url"],
                "status": interview["status"],
            }

        response_applications.append(
            {
                "id": application["id"],
                "job_id": application["job_id"],
                "status": application["status"],
                "applied_at": application["applied_at"],
                "job": job,
                "interview": safe_interview,
            }
        )

    return {"applications": response_applications}


@app.put("/my-applications/{application_id}/withdraw")
async def withdraw_my_application(
    application_id: str,
    authorization: str = Header(None),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Candidate profile required")

    if not profile_result.data:
        raise HTTPException(status_code=403, detail="Candidate profile required")
    if profile_result.data[0]["role"] != "candidate":
        raise HTTPException(status_code=403, detail="Only candidates can withdraw applications")

    try:
        application_result = (
            user_supabase.table("applications")
            .select("id,status,candidate_id")
            .eq("id", application_id)
            .eq("candidate_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=404, detail="Application not found")

    if not application_result.data:
        raise HTTPException(status_code=404, detail="Application not found")

    application = application_result.data[0]
    final_statuses = ["hired", "rejected", "withdrawn"]
    if application["status"] in final_statuses:
        raise HTTPException(
            status_code=400,
            detail="Application cannot be withdrawn in its current state",
        )

    try:
        update_result = (
            user_supabase.table("applications")
            .update({"status": "withdrawn"})
            .eq("id", application_id)
            .eq("candidate_id", user_id)
            .in_("status", ["applied", "shortlisted", "interview"])
            .select("id,job_id,status")
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not withdraw application: {e}")

    if not update_result.data:
        raise HTTPException(status_code=400, detail="Application could not be withdrawn")

    return {
        "message": "Application withdrawn successfully",
        "application": update_result.data[0],
    }


@app.post("/admin/recruiters")
async def create_recruiter(request: Request, authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Admin profile required")

    if not profile_result.data or profile_result.data[0]["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create recruiters")

    data = await request.json()
    full_name = str(data.get("full_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    if not full_name or not email:
        raise HTTPException(status_code=422, detail="full_name and email are required")

    try:
        admin_supabase = create_admin_client()
    except HTTPException:
        raise

    try:
        invited = admin_supabase.auth.admin.invite_user_by_email(
            email,
            {
                "data": {
                    "full_name": full_name,
                    "role": "recruiter",
                }
            },
        )
        if not invited.user:
            raise RuntimeError("Supabase did not return the invited user")

        recruiter_id = invited.user.id
        profile_result = (
            admin_supabase.table("profiles")
            .upsert(
                {
                    "id": recruiter_id,
                    "full_name": full_name,
                    "email": email,
                    "role": "recruiter",
                    "is_active": True,
                },
                on_conflict="id",
            )
            .execute()
        )
        if not profile_result.data:
            raise RuntimeError("Recruiter profile was not created")
    except Exception as e:
        message = str(e).lower()
        if "already registered" in message or "already exists" in message or "duplicate" in message:
            raise HTTPException(status_code=409, detail="Recruiter email already exists")
        try:
            if "recruiter_id" in locals():
                admin_supabase.auth.admin.delete_user(recruiter_id)
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"Could not create recruiter: {e}")

    return {
        "message": "Recruiter invitation sent successfully",
        "recruiter": {
            "id": recruiter_id,
            "full_name": full_name,
            "email": email,
            "role": "recruiter",
            "is_active": True,
        },
    }


@app.get("/admin/recruiters")
async def list_recruiters(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Admin profile required")

    if not profile_result.data or profile_result.data[0]["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can list recruiters")

    admin_supabase = create_admin_client()
    try:
        result = (
            admin_supabase.table("profiles")
            .select("id,full_name,email,is_active,created_at")
            .eq("role", "recruiter")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load recruiters: {e}")

    return {"recruiters": result.data or []}


@app.put("/admin/recruiters/{recruiter_id}/status")
async def update_recruiter_status(
    recruiter_id: str,
    request: Request,
    authorization: str = Header(None),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        admin_profile = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Admin profile required")

    if not admin_profile.data or admin_profile.data[0]["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can change recruiter status")

    data = await request.json()
    is_active = data.get("is_active")
    if not isinstance(is_active, bool):
        raise HTTPException(status_code=422, detail="is_active must be a boolean")

    admin_supabase = create_admin_client()
    try:
        recruiter_result = (
            admin_supabase.table("profiles")
            .select("id,role,is_active")
            .eq("id", recruiter_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load recruiter: {e}")

    if not recruiter_result.data or recruiter_result.data[0].get("role") != "recruiter":
        raise HTTPException(status_code=404, detail="Recruiter not found")

    try:
        updated = (
            admin_supabase.table("profiles")
            .update({"is_active": is_active})
            .eq("id", recruiter_id)
            .eq("role", "recruiter")
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not update recruiter status: {e}")

    if not updated.data:
        raise HTTPException(status_code=404, detail="Recruiter not found")

    return {
        "message": (
            "Recruiter activated successfully"
            if is_active
            else "Recruiter deactivated successfully"
        ),
        "recruiter": {
            "id": recruiter_id,
            "is_active": is_active,
        },
    }


@app.post("/admin/jobs/{job_id}/recruiters/{recruiter_id}")
async def assign_recruiter_to_job(
    job_id: str,
    recruiter_id: str,
    authorization: str = Header(None),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        admin_profile = (
            user_supabase.table("profiles")
            .select("role")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Admin profile required")

    if not admin_profile.data or admin_profile.data[0]["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can assign recruiters")

    admin_supabase = create_admin_client()

    try:
        job_result = (
            admin_supabase.table("jobs")
            .select("id")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load job: {e}")

    if not job_result.data:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        recruiter_result = (
            admin_supabase.table("profiles")
            .select("id,role,is_active")
            .eq("id", recruiter_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load recruiter: {e}")

    if not recruiter_result.data or recruiter_result.data[0].get("role") != "recruiter":
        raise HTTPException(status_code=404, detail="Recruiter not found")

    recruiter = recruiter_result.data[0]
    if not recruiter.get("is_active", True):
        raise HTTPException(status_code=409, detail="Recruiter is inactive")

    try:
        existing = (
            admin_supabase.table("recruiter_jobs")
            .select("id")
            .eq("job_id", job_id)
            .eq("recruiter_id", recruiter_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not check assignment: {e}")

    if existing.data:
        raise HTTPException(status_code=409, detail="Recruiter is already assigned to this job")

    try:
        assignment_result = (
            admin_supabase.table("recruiter_jobs")
            .insert({"job_id": job_id, "recruiter_id": recruiter_id})
            .execute()
        )
    except Exception as e:
        error_text = str(e).lower()
        if "duplicate" in error_text or "unique" in error_text:
            raise HTTPException(status_code=409, detail="Recruiter is already assigned to this job")
        raise HTTPException(status_code=400, detail=f"Could not assign recruiter: {e}")

    if not assignment_result.data:
        raise HTTPException(status_code=400, detail="Recruiter assignment failed")

    assignment = assignment_result.data[0]
    return {
        "message": "Recruiter assigned successfully",
        "assignment": {
            "id": assignment["id"],
            "job_id": assignment["job_id"],
            "recruiter_id": assignment["recruiter_id"],
            "assigned_at": assignment.get("assigned_at"),
        },
    }


@app.get("/recruiter/jobs")
async def list_recruiter_jobs(authorization: str = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role,is_active")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Recruiter profile required")

    if not profile_result.data or profile_result.data[0].get("role") != "recruiter":
        raise HTTPException(status_code=403, detail="Only recruiters can view assigned jobs")

    if not profile_result.data[0].get("is_active", True):
        raise HTTPException(status_code=403, detail="Recruiter account is inactive")

    try:
        assignments_result = (
            user_supabase.table("recruiter_jobs")
            .select("job_id,assigned_at")
            .eq("recruiter_id", user_id)
            .order("assigned_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load recruiter assignments: {e}")

    assignments = assignments_result.data or []
    if not assignments:
        return {"jobs": []}

    job_ids = [assignment["job_id"] for assignment in assignments]
    try:
        jobs_result = (
            user_supabase.table("jobs")
            .select(
                "id,title,company_name,department,location,employment_type,status,last_date,openings"
            )
            .in_("id", job_ids)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load assigned jobs: {e}")

    jobs_by_id = {job["id"]: job for job in (jobs_result.data or [])}
    jobs = []
    for assignment in assignments:
        job = jobs_by_id.get(assignment["job_id"])
        if not job:
            continue
        jobs.append({**job, "assigned_at": assignment["assigned_at"]})

    return {"jobs": jobs}


@app.get("/recruiter/jobs/{job_id}/applications")
async def list_recruiter_job_applications(
    job_id: str,
    authorization: str = Header(None),
):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token required")

    try:
        user_response = supabase.auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    if not user_response.user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    recruiter_id = user_response.user.id
    user_supabase = create_client(url, key)
    user_supabase.postgrest.auth(token)

    try:
        profile_result = (
            user_supabase.table("profiles")
            .select("role,is_active")
            .eq("id", recruiter_id)
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(status_code=403, detail="Recruiter profile required")

    if not profile_result.data or profile_result.data[0].get("role") != "recruiter":
        raise HTTPException(status_code=403, detail="Only recruiters can view applications")

    if not profile_result.data[0].get("is_active", True):
        raise HTTPException(status_code=403, detail="Recruiter account is inactive")

    try:
        job_result = (
            user_supabase.table("jobs")
            .select("id")
            .eq("id", job_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load job: {e}")

    if not job_result.data:
        raise HTTPException(status_code=404, detail="Job not found")

    try:
        assignment_result = (
            user_supabase.table("recruiter_jobs")
            .select("job_id")
            .eq("recruiter_id", recruiter_id)
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not verify job assignment: {e}")

    if not assignment_result.data:
        raise HTTPException(status_code=403, detail="Recruiter is not assigned to this job")

    try:
        applications_result = (
            user_supabase.table("applications")
            .select("id,job_id,candidate_id,resume_path,status,applied_at")
            .eq("job_id", job_id)
            .order("applied_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load applications: {e}")

    applications = applications_result.data or []
    if not applications:
        return {"applications": []}

    # Candidate profiles are private under the existing RLS policy. Use the
    # backend-only admin client for safe profile fields when applications exist.
    admin_supabase = create_admin_client()
    candidate_ids = list({application["candidate_id"] for application in applications})
    try:
        profiles_result = (
            admin_supabase.table("profiles")
            .select("id,full_name,email,phone")
            .in_("id", candidate_ids)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load candidate profiles: {e}")

    profiles_by_id = {profile["id"]: profile for profile in (profiles_result.data or [])}
    response_applications = []
    for application in applications:
        candidate_id = application["candidate_id"]
        response_applications.append(
            {
                "id": application["id"],
                "job_id": application["job_id"],
                "status": application["status"],
                "applied_at": application["applied_at"],
                "candidate": profiles_by_id.get(
                    candidate_id,
                    {"id": candidate_id, "full_name": None, "email": None, "phone": None},
                ),
                "has_cv": bool(application.get("resume_path")),
            }
        )

    return {"applications": response_applications}


@app.get("/recruiter/applications/{application_id}/cv")
async def get_recruiter_application_cv(
    application_id: str,
    authorization: str = Header(None),
):
    _, recruiter_id, user_supabase = require_active_recruiter(authorization)
    application = require_assigned_application(user_supabase, recruiter_id, application_id)
    resume_path = application.get("resume_path")
    if not resume_path:
        raise HTTPException(status_code=404, detail="CV snapshot not found")

    admin_supabase = create_admin_client()
    try:
        signed_result = admin_supabase.storage.from_("cvs").create_signed_url(
            resume_path,
            300,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not create CV signed URL: {e}")

    signed_url = signed_result.get("signedURL") or signed_result.get("signedUrl")
    if not signed_url:
        raise HTTPException(status_code=400, detail="Could not create CV signed URL")
    return {"signed_url": signed_url, "expires_in": 300}


@app.get("/recruiter/applications/{application_id}/notes")
async def get_recruiter_application_notes(
    application_id: str,
    authorization: str = Header(None),
):
    _, recruiter_id, user_supabase = require_active_recruiter(authorization)
    require_assigned_application(user_supabase, recruiter_id, application_id)
    try:
        result = (
            user_supabase.table("recruiter_notes")
            .select("id,note,created_at")
            .eq("application_id", application_id)
            .eq("recruiter_id", recruiter_id)
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load notes: {e}")
    return {"notes": result.data or []}


@app.post("/recruiter/applications/{application_id}/notes")
async def add_recruiter_application_note(
    application_id: str,
    request: Request,
    authorization: str = Header(None),
):
    _, recruiter_id, user_supabase = require_active_recruiter(authorization)
    require_assigned_application(user_supabase, recruiter_id, application_id)
    data = await request.json()
    note = str(data.get("note", "")).strip()
    if not note:
        raise HTTPException(status_code=422, detail="note is required")

    try:
        result = (
            user_supabase.table("recruiter_notes")
            .insert(
                {
                    "application_id": application_id,
                    "recruiter_id": recruiter_id,
                    "note": note,
                }
            )
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not save note: {e}")
    if not result.data:
        raise HTTPException(status_code=400, detail="Note was not saved")
    saved = result.data[0]
    return {
        "message": "Note added successfully",
        "note": {
            "id": saved["id"],
            "note": saved["note"],
            "created_at": saved["created_at"],
        },
    }


@app.put("/recruiter/applications/{application_id}/stage")
async def update_recruiter_application_stage(
    application_id: str,
    request: Request,
    authorization: str = Header(None),
):
    _, recruiter_id, user_supabase = require_active_recruiter(authorization)
    application = require_assigned_application(user_supabase, recruiter_id, application_id)
    data = await request.json()
    new_status = str(data.get("status", "")).strip().lower()
    allowed_statuses = {"shortlisted", "offer", "hired", "rejected"}
    if new_status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="status must be shortlisted, offer, hired, or rejected",
        )

    current_status = application["status"]
    if current_status in {"hired", "rejected", "withdrawn"}:
        raise HTTPException(status_code=409, detail="Final applications cannot be changed")
    if new_status == "interview":
        raise HTTPException(
            status_code=409,
            detail="Interview status is set only when an interview is scheduled",
        )

    transitions = {
        "applied": {"shortlisted", "rejected"},
        "shortlisted": {"rejected"},
        "interview": {"offer", "rejected"},
        "offer": {"hired", "rejected"},
    }
    if new_status not in transitions.get(current_status, set()):
        raise HTTPException(status_code=409, detail="Invalid application stage transition")

    try:
        job_result = (
            user_supabase.table("jobs")
            .select("id,status,openings")
            .eq("id", application["job_id"])
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load job: {e}")
    if not job_result.data:
        raise HTTPException(status_code=404, detail="Job not found")
    job = job_result.data[0]
    if job["status"] == "closed" and new_status != "rejected":
        raise HTTPException(status_code=409, detail="Closed jobs cannot progress applications")

    if new_status == "hired" and not service_role_key:
        raise HTTPException(
            status_code=503,
            detail="Hiring workflow requires SUPABASE_SERVICE_ROLE_KEY for job auto-close",
        )

    try:
        updated = (
            user_supabase.table("applications")
            .update({"status": new_status})
            .eq("id", application_id)
            .eq("status", current_status)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not update application stage: {e}")
    if not updated.data:
        raise HTTPException(status_code=409, detail="Application stage changed; retry the request")

    if new_status == "hired":
        admin_supabase = create_admin_client()
        try:
            hired_result = (
                admin_supabase.table("applications")
                .select("id")
                .eq("job_id", application["job_id"])
                .eq("status", "hired")
                .execute()
            )
            if len(hired_result.data or []) >= int(job["openings"]):
                admin_supabase.table("jobs").update({"status": "closed"}).eq(
                    "id", application["job_id"]
                ).execute()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Application hired but job close check failed: {e}")

    if new_status in {"hired", "rejected"}:
        automation_context = get_automation_context(application_id, application["job_id"])
        await send_n8n_webhook(
            n8n_status_webhook_url,
            {
                "event": f"application_{new_status}",
                "application_id": application_id,
                "job_id": application["job_id"],
                "candidate_id": application["candidate_id"],
                "candidate_name": automation_context["candidate_name"],
                "candidate_email": automation_context["candidate_email"],
                "job_title": automation_context["job_title"],
                "status": new_status,
            },
        )

    return {"message": "Application stage updated successfully", "application": updated.data[0]}


@app.post("/recruiter/applications/{application_id}/interview")
async def schedule_recruiter_interview(
    application_id: str,
    request: Request,
    authorization: str = Header(None),
):
    _, recruiter_id, user_supabase = require_active_recruiter(authorization)
    application = require_assigned_application(user_supabase, recruiter_id, application_id)
    if application["status"] != "shortlisted":
        raise HTTPException(status_code=409, detail="Only shortlisted applications can be scheduled")

    data = await request.json()
    interview_date = str(data.get("interview_date", "")).strip()
    interview_time = str(data.get("interview_time", "")).strip()
    if not interview_date or not interview_time:
        raise HTTPException(status_code=422, detail="interview_date and interview_time are required")
    try:
        scheduled_at = datetime.fromisoformat(f"{interview_date}T{interview_time}")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid interview date or time")
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=timezone.utc)
    start = scheduled_at.astimezone(timezone.utc)
    end = start + timedelta(hours=1)
    if start <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="Interview cannot be scheduled in the past")

    location = str(data.get("location", "")).strip()
    meeting_url = str(data.get("meeting_url", "")).strip()
    if not location and not meeting_url:
        raise HTTPException(status_code=422, detail="location or meeting_url is required")

    try:
        existing_result = (
            user_supabase.table("interviews")
            .select("id,scheduled_at,status")
            .eq("interviewer_id", recruiter_id)
            .eq("status", "scheduled")
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not check interview schedule: {e}")

    for existing in existing_result.data or []:
        try:
            existing_start = datetime.fromisoformat(existing["scheduled_at"].replace("Z", "+00:00"))
            if existing_start.tzinfo is None:
                existing_start = existing_start.replace(tzinfo=timezone.utc)
            existing_end = existing_start + timedelta(hours=1)
            if start < existing_end and end > existing_start:
                raise HTTPException(status_code=409, detail="Interview time overlaps an existing interview")
        except HTTPException:
            raise
        except (ValueError, AttributeError):
            continue

    interview = {
        "application_id": application_id,
        "interviewer_id": recruiter_id,
        "scheduled_at": start.isoformat(),
        "mode": "online" if meeting_url else "in_person",
        "meeting_url": meeting_url or None,
        "notes": location or None,
        "status": "scheduled",
    }
    try:
        interview_result = user_supabase.table("interviews").insert(interview).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not schedule interview: {e}")
    if not interview_result.data:
        raise HTTPException(status_code=400, detail="Interview was not scheduled")

    try:
        updated = (
            user_supabase.table("applications")
            .update({"status": "interview"})
            .eq("id", application_id)
            .eq("status", "shortlisted")
            .execute()
        )
        if not updated.data:
            raise RuntimeError("Application is no longer shortlisted")
    except Exception as e:
        user_supabase.table("interviews").delete().eq("id", interview_result.data[0]["id"]).execute()
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=409, detail=f"Could not update application to interview: {e}")

    scheduled = interview_result.data[0]
    automation_context = get_automation_context(application_id, application["job_id"])
    await send_n8n_webhook(
        n8n_interview_webhook_url,
        {
            "event": "interview_scheduled",
            "application_id": application_id,
            "job_id": application["job_id"],
            "candidate_id": application["candidate_id"],
            "candidate_name": automation_context["candidate_name"],
            "candidate_email": automation_context["candidate_email"],
            "job_title": automation_context["job_title"],
            "scheduled_at": scheduled["scheduled_at"],
            "mode": scheduled["mode"],
            "meeting_url": scheduled["meeting_url"],
            "location": scheduled.get("notes"),
        },
    )
    return {
        "message": "Interview scheduled successfully",
        "interview": {
            "id": scheduled["id"],
            "application_id": scheduled["application_id"],
            "scheduled_at": scheduled["scheduled_at"],
            "mode": scheduled["mode"],
            "meeting_url": scheduled["meeting_url"],
            "status": scheduled["status"],
        },
        "application": updated.data[0],
    }


@app.post("/recruiter/applications/{application_id}/ai-summary/retry")
async def retry_application_ai_summary(
    application_id: str,
    authorization: str = Header(None),
):
    _, recruiter_id, user_supabase = require_active_recruiter(authorization)
    application = require_assigned_application(user_supabase, recruiter_id, application_id)
    now = datetime.now(timezone.utc).isoformat()
    try:
        updated = (
            user_supabase.table("applications")
            .update(
                {
                    "ai_status": "pending",
                    "ai_summary": None,
                    "ai_error": None,
                    "ai_updated_at": now,
                }
            )
            .eq("id", application_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not queue AI retry: {e}")
    if not updated.data:
        raise HTTPException(status_code=400, detail="AI retry was not queued")

    context = get_automation_context(application_id, application["job_id"])
    await send_n8n_webhook(
        n8n_ai_webhook_url,
        {
            "event": "ai_summary_retry",
            "application_id": application_id,
            "job_id": application["job_id"],
            "candidate_id": application["candidate_id"],
            "resume_path": application.get("resume_path"),
            "cv_signed_url": create_storage_signed_url(application.get("resume_path")),
            "cv_url_expires_in": 300,
            "job_title": context["job_title"],
            "job_requirements": context["job_requirements"],
        },
    )
    return {"message": "AI summary retry requested", "status": "pending"}


@app.get("/recruiter/applications/{application_id}/ai-summary")
async def get_application_ai_summary(
    application_id: str,
    authorization: str = Header(None),
):
    _, recruiter_id, user_supabase = require_active_recruiter(authorization)
    require_assigned_application(user_supabase, recruiter_id, application_id)
    try:
        result = (
            user_supabase.table("applications")
            .select("ai_status,ai_summary")
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load AI summary: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Application not found")
    row = result.data[0]
    if row.get("ai_status") != "completed" or not row.get("ai_summary"):
        return {
            "status": row.get("ai_status") or "failed",
            "ai_generated": True,
            "summary": None,
            "message": "Summary not available",
        }
    return {
        "status": "completed",
        "ai_generated": True,
        "summary": row["ai_summary"],
    }


@app.post("/internal/ai-summary")
async def save_ai_summary(
    request: Request,
    callback_secret: str = Header(None, alias="x-n8n-callback-secret"),
):
    if not n8n_callback_secret:
        raise HTTPException(status_code=503, detail="AI callback is not configured")
    if not callback_secret or callback_secret != n8n_callback_secret:
        raise HTTPException(status_code=401, detail="Invalid callback secret")

    data = await request.json()
    application_id = str(data.get("application_id", "")).strip()
    status = str(data.get("status", "")).strip().lower()
    if not application_id or status not in {"completed", "failed"}:
        raise HTTPException(status_code=422, detail="application_id and valid status are required")

    summary = data.get("summary")
    if status == "completed":
        if not isinstance(summary, dict):
            raise HTTPException(status_code=422, detail="summary must be an object")
        forbidden = {"score", "ranking", "recommendation", "hire_recommendation"}
        if forbidden.intersection(summary.keys()):
            raise HTTPException(status_code=422, detail="AI summary contains forbidden fields")
        profile_bullets = summary.get("profile_bullets")
        requirements_mentioned = summary.get("requirements_mentioned")
        requirements_missing = summary.get("requirements_missing")
        interview_questions = summary.get("interview_questions")
        if not (
            isinstance(profile_bullets, list)
            and 3 <= len(profile_bullets) <= 5
            and all(isinstance(item, str) and item.strip() for item in profile_bullets)
            and isinstance(requirements_mentioned, list)
            and all(isinstance(item, str) for item in requirements_mentioned)
            and isinstance(requirements_missing, list)
            and all(isinstance(item, str) for item in requirements_missing)
            and isinstance(interview_questions, list)
            and len(interview_questions) == 3
            and all(isinstance(item, str) and item.strip() for item in interview_questions)
        ):
            raise HTTPException(status_code=422, detail="AI summary structure is invalid")
    else:
        summary = None

    admin_supabase = create_admin_client()
    update_values = {
        "ai_status": status,
        "ai_summary": summary,
        "ai_error": data.get("error") if status == "failed" else None,
        "ai_updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        result = (
            admin_supabase.table("applications")
            .update(update_values)
            .eq("id", application_id)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not save AI summary: {e}")
    if not result.data:
        raise HTTPException(status_code=404, detail="Application not found")
    return {"message": "AI summary result saved", "status": status}


@app.post("/internal/application-cv-url")
async def create_internal_application_cv_url(
    request: Request,
    callback_secret: str = Header(None, alias="x-n8n-callback-secret"),
):
    if not n8n_callback_secret:
        raise HTTPException(status_code=503, detail="n8n callback is not configured")
    if not callback_secret or callback_secret != n8n_callback_secret:
        raise HTTPException(status_code=401, detail="Invalid callback secret")

    data = await request.json()
    application_id = str(data.get("application_id", "")).strip()
    if not application_id:
        raise HTTPException(status_code=422, detail="application_id is required")

    admin_supabase = create_admin_client()
    try:
        result = (
            admin_supabase.table("applications")
            .select("resume_path")
            .eq("id", application_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not load CV snapshot: {e}")
    if not result.data or not result.data[0].get("resume_path"):
        raise HTTPException(status_code=404, detail="CV snapshot not found")

    try:
        signed_result = admin_supabase.storage.from_("cvs").create_signed_url(
            result.data[0]["resume_path"],
            300,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not create CV signed URL: {e}")
    signed_url = signed_result.get("signedURL") or signed_result.get("signedUrl")
    if not signed_url:
        raise HTTPException(status_code=400, detail="Could not create CV signed URL")
    return {"signed_url": signed_url, "expires_in": 300}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=os.getenv("UVICORN_RELOAD", "false").lower() == "true",
    )

