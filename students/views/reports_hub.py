"""
Unified Reports Hub — premium landing page combining Results and Report Cards.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Avg
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache

from .helpers import (
    get_class_teacher_scope,
    get_preview_contexts_for_user,
    get_published_contexts_for_user,
    get_teacher_for_user,
    user_has_main_school_admin_override,
)
from ..models import Mark, MarkSubmission, Student
from ..security import (
    get_request_school,
    get_request_school_section,
)


@login_required(login_url='login')
@never_cache
def reports_hub(request):
    """
    Unified reports hub with two tabs: Results and Report Cards.
    Shows summary stats, context cards with mini previews, and premium UI.
    """
    school = get_request_school(request)
    if not school:
        messages.error(request, "School context is required.")
        return redirect('welcome_page')

    section = get_request_school_section(request)
    is_lower_primary = section == 'LOWER_PRIMARY'
    is_primary = section == 'PRIMARY' or is_lower_primary
    is_admin = user_has_main_school_admin_override(request.user)
    teacher = get_teacher_for_user(request.user)
    class_scope = get_class_teacher_scope(teacher)

    # ── Published contexts for Results tab (all teachers) ──
    results_contexts = get_published_contexts_for_user(request.user)
    if is_admin:
        preview_contexts = get_preview_contexts_for_user(request.user)
        published_keys = {c["context_key"] for c in results_contexts}
        for pc in preview_contexts:
            if pc["context_key"] not in published_keys:
                pc["is_preview_only"] = True
                results_contexts.append(pc)

    # ── Published contexts for Report Cards tab (admins + class teachers) ──
    report_card_contexts = get_published_contexts_for_user(
        request.user, require_class_teacher=True
    )

    # ── Mini preview per results context (top 5, class mean) ──
    for ctx in results_contexts:
        year = str(ctx["year"])
        term = ctx["term"]
        grade = ctx["class_name"]
        stream = ctx["stream"]
        exam = ctx["exam_name"]

        marks_qs = Mark.objects.filter(
            school=school,
            year=year, term=term, exam_type=exam,
            student__class_name=grade, student__stream=stream,
        ).exclude(is_absent=True)

        # Class mean
        agg = marks_qs.aggregate(avg=Avg('score'))
        ctx['class_mean'] = round(agg['avg'], 1) if agg['avg'] else 0

        # Student count
        ctx['student_count'] = Student.objects.filter(
            school=school, class_name=grade, stream=stream,
        ).count()

        # Top 3 students by total points
        student_totals = (
            marks_qs
            .values('student__id', 'student__name')
            .annotate(total=Avg('score'))
            .order_by('-total')[:3]
        )
        ctx['top_students'] = [
            {'name': s['student__name'], 'avg': round(s['total'], 1)}
            for s in student_totals
        ]

    # ── Summary stats ──
    total_results = len(results_contexts)
    total_report_cards = len(report_card_contexts)
    pending_submissions = MarkSubmission.objects.filter(
        school=school, status='in_progress'
    ).count() if school else 0

    # Group results context by exam_name for cleaner display
    results_by_exam = {}
    for ctx in results_contexts:
        exam = ctx['exam_name']
        if exam not in results_by_exam:
            results_by_exam[exam] = {
                'exam_name': exam,
                'contexts': [],
            }
        results_by_exam[exam]['contexts'].append(ctx)

    return render(request, 'students/reports_hub.html', {
        'results_contexts': results_contexts,
        'results_by_exam': results_by_exam,
        'report_card_contexts': report_card_contexts,
        'total_results': total_results,
        'total_report_cards': total_report_cards,
        'pending_submissions': pending_submissions,
        'is_admin': is_admin,
        'is_primary': is_primary,
        'class_scope': class_scope,
        'active_tab': request.GET.get('tab', 'results'),
    })
