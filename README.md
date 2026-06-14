# EduNexus Exam System

A multi-tenant school exam management system built with Django. Handles student registration, mark entry, broadsheet analysis, report card generation, and PDF exports for both **Primary** (Grade 4-6) and **Junior Secondary** (Grade 7-9) sections.

## Features

- **Multi-tenant** — Multiple schools on one deployment, each with isolated data
- **Dual section support** — Primary (CBC 4-level grading) and JSS (KJSEA 8-level grading)
- **Mark entry** — Teachers enter marks per subject, with automatic performance level calculation
- **Broadsheet** — Class-wide analysis with subject averages, distributions, and PLV
- **Report cards** — Individual and bulk report cards with class teacher/headteacher remarks
- **PDF export** — Playwright-based PDF generation for broadsheets and report cards
- **Religion-aware** — CRE/IRE mutually exclusive for both Primary and JSS
- **Comment freeze** — Teacher remarks stay editable for 30 days, then freeze in report cards
- **Role-based access** — School admin, class teacher, and subject teacher roles

## Prerequisites

- Python 3.13+
- PostgreSQL 14+
- Redis (for Celery, optional for development)

## Installation

1. **Clone the repository**
   ```bash
   git clone https://github.com/your-username/exam-system.git
   cd exam-system
   ```

2. **Create virtual environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your actual values
   ```

5. **Set up the database**
   ```bash
   # Create PostgreSQL database
   createdb school_exam_db

   # Run migrations
   python manage.py migrate

   # Create superuser
   python manage.py createsuperuser
   ```

6. **Seed primary subjects** (optional)
   ```bash
   python manage.py seed_primary_subjects
   ```

7. **Run the development server**
   ```bash
   python manage.py runserver
   ```

## Project Structure

```
exam-system/
├── school/              # Django project settings
│   ├── settings.py      # Main settings (reads from .env)
│   ├── urls.py          # Root URL configuration
│   ├── celery.py        # Celery configuration
│   └── wsgi.py
├── students/            # Main application
│   ├── models.py        # Student, Mark, Subject, Comment models
│   ├── views/           # View modules (split by concern)
│   │   ├── reports.py   # Broadheet and report card views
│   │   ├── exams.py     # Mark entry and exam management
│   │   ├── faculty.py   # Comments, learner profiles
│   │   ├── helpers.py   # Shared utilities
│   │   └── constants.py # Subject maps, assessment maps
│   ├── templates/       # HTML templates
│   ├── static/          # CSS, JS, images
│   ├── migrations/      # Database migrations
│   └── management/      # Management commands
├── static/              # Project-level static files
├── superuser/           # Superuser management
├── manage.py
├── requirements.txt
├── .env.example
└── .gitignore
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DJANGO_SECRET_KEY` | Yes | — | Django secret key |
| `DJANGO_DEBUG` | No | `True` | Debug mode |
| `DB_NAME` | No | `school_exam_db` | PostgreSQL database name |
| `DB_USER` | No | `postgres` | Database user |
| `DB_PASSWORD` | Yes | — | Database password |
| `DB_HOST` | No | `127.0.0.1` | Database host |
| `DB_PORT` | No | `5432` | Database port |
| `CELERY_BROKER_URL` | No | `redis://127.0.0.1:6379/0` | Redis URL for Celery |
| `EMAIL_HOST_USER` | No | — | Gmail address for notifications |
| `EMAIL_HOST_PASSWORD` | No | — | Gmail app password |

## Usage

### School Admin
- Create and manage exams (name them freely — e.g., "Opener", "Mid Term", "CAT 1")
- Review and publish teacher submissions
- Configure headteacher remarks per performance level
- View broadsheets and generate report cards

### Class Teacher
- Enter marks for assigned subjects
- Configure class teacher remarks per performance level
- Set term opening/closing dates
- Generate individual or bulk report cards

### Subject Teacher
- Enter marks for assigned subjects
- View published results

## License

This project is proprietary software. All rights reserved.
