"""
Enhanced admin configuration for the EduNexus system.

Provides professional grouped student view, teacher management,
and school administration panels.
"""

from django.contrib import admin
from django.utils.safestring import mark_safe
from django.contrib.auth.models import User
from django.db.models import Count
from django.contrib.auth.admin import UserAdmin
from django.urls import path
from django.shortcuts import render
from unfold.admin import ModelAdmin
from django.utils.html import format_html
from django.utils.timezone import now
from .models import (
    ClassTeacherMasterComment,
    Guardian,
    Mark,
    MarkSubmission,
    School,
    SecurityAuditLog,
    Student,
    SubjectAssignment,
    Teacher,
)


class SchoolScopedAdminMixin:
    """Platform admins see all tenants; school-scoped queries use all_objects."""

    def get_queryset(self, request):
        qs = self.model.all_objects.get_queryset() if hasattr(self.model, "all_objects") else super().get_queryset(request)
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

# ==============================================================================
# 🎨 DYNAMIC GLOBAL EDUNEXUS DASHBOARD CONFIGURATION
# ==============================================================================
admin.site.site_header = "EDUNEXUS Examination System"
admin.site.site_title = "EDUNEXUS Admin Portal"
admin.site.index_title = "Welcome to your School Management Dashboard"

# ================================================================================
# ⚠️ CRITICAL FIX: Unregister the default User admin BEFORE registering Teacher
# ================================================================================
admin.site.unregister(User)


# -------------------- Guardian Admin --------------------
@admin.register(Guardian)
class GuardianAdmin(SchoolScopedAdminMixin, ModelAdmin):
    list_display = ['name', 'phone']
    search_fields = ['name', 'phone']

    def log_addition(self, request, object, message): pass
    def log_change(self, request, object, message): pass
    def log_deletion(self, request, object, object_repr): pass


# ==============================================================================
# 🌟 ENHANCED STUDENT ADMIN - WITH GROUPED CLASS VIEW
# ==============================================================================
@admin.register(Student)
class StudentAdmin(SchoolScopedAdminMixin, ModelAdmin):
    """
    🌟 PROFESSIONAL STUDENT ADMIN WITH HIERARCHICAL NAVIGATION
    - First view shows class/stream groupings
    - Click on a group to view students in that class
    - Clean, organized, easy to navigate
    """
    
    # ============ LIST DISPLAY ============
    list_display = ['admission_no', 'name', 'class_name', 'stream', 'term', 'year']
    list_display_links = ['admission_no', 'name']
    ordering = ['admission_no']

    # ============ FILTERING & SEARCH ============
    list_filter = ['class_name', 'stream', 'year', 'term']
    search_fields = ['name', 'admission_no']

    # ============ METRICS ============
    list_metrics = ['g7_yellow', 'g7_blue', 'g8_main']

    def get_urls(self):
        """Add custom URL for grouped class view"""
        urls = super().get_urls()
        custom_urls = [
            path(
                'class-groups/',
                self.admin_site.admin_view(self.class_groups_view),
                name='students_student_classgroups',
            ),
        ]
        return custom_urls + urls

    def class_groups_view(self, request):
        """
        Display students grouped by Class and Stream
        Clicking on a group filters to show only those students
        """
        # Get all distinct class/stream combinations with student counts
        class_groups = (
            Student.all_objects
            .values('class_name', 'stream')
            .annotate(count=Count('id'))
            .order_by('class_name', 'stream')
        )

        # Enrich with additional data
        groups_with_details = []
        for group in class_groups:
            class_name = group['class_name']
            stream = group['stream']
            count = group['count']
            
            # Get the URL to filter by this class/stream
            filter_url = f"/admin/students/student/?class_name__exact={class_name.replace(' ', '+')}&stream__exact={stream}"
            
            # Determine icon and color based on class
            if 'Grade 7' in class_name:
                icon = '🟡'
                color = '#FCD34D'
            elif 'Grade 8' in class_name:
                icon = '🔵'
                color = '#60A5FA'
            else:
                icon = '🟢'
                color = '#34D399'
            
            groups_with_details.append({
                'class_name': class_name,
                'stream': stream,
                'display_name': f"{class_name} - {stream}",
                'count': count,
                'filter_url': filter_url,
                'icon': icon,
                'color': color,
            })

        context = {
            'title': 'Students by Class & Stream',
            'groups': groups_with_details,
            'total_students': sum(g['count'] for g in groups_with_details),
        }

        return render(request, 'admin/student_class_groups.html', context)

    def changelist_view(self, request, extra_context=None):
        """Override to add custom context and button to view grouped classes"""
        extra_context = extra_context or {}
        extra_context['show_class_groups_button'] = True
        extra_context['class_groups_url'] = 'admin:students_student_classgroups'
        return super().changelist_view(request, extra_context=extra_context)

    # ============ METRIC CARDS ============
    def get_list_metrics(self, request):
        queryset = self.get_queryset(request)
        base_url = request.path
        
        return {
            "g7_yellow": {
                "title": "Grade 7 Yellow",
                "metric": queryset.filter(class_name="Grade 7", stream="Yellow").count(),
                "icon": "school",
                "path": f"{base_url}?class_name__exact=Grade+7&stream__exact=Yellow"
            },
            "g7_blue": {
                "title": "Grade 7 Blue",
                "metric": queryset.filter(class_name="Grade 7", stream="Blue").count(),
                "icon": "school",
                "path": f"{base_url}?class_name__exact=Grade+7&stream__exact=Blue"
            },
            "g8_main": {
                "title": "Grade 8 Main",
                "metric": queryset.filter(class_name="Grade 8", stream="Main").count(),
                "icon": "school",
                "path": f"{base_url}?class_name__exact=Grade+8&stream__exact=Main"
            },
        }

    def log_addition(self, request, object, message): pass
    def log_change(self, request, object, message): pass
    def log_deletion(self, request, object, object_repr): pass


# -------------------- Mark Admin --------------------
@admin.register(Mark)
class MarkAdmin(SchoolScopedAdminMixin, ModelAdmin):
    list_display = ['student', 'exam_type', 'score', 'year']
    list_filter = ['exam_type', 'year']
    search_fields = ['student__name', 'student__admission_no']


# -------------------- Mark Submission Admin --------------------
@admin.register(MarkSubmission)
class MarkSubmissionAdmin(SchoolScopedAdminMixin, ModelAdmin):
    list_display = ['teacher', 'subject', 'class_name', 'stream', 'exam_name', 'term', 'year', 'status', 'submitted_at']
    list_filter = ['status', 'exam_name', 'term', 'year']
    search_fields = ['teacher__user__first_name', 'teacher__user__last_name', 'subject__name', 'class_name']
    readonly_fields = ['submitted_at', 'reviewed_at', 'published_at']


# -------------------- Class Teacher Master Comment Admin --------------------
@admin.register(ClassTeacherMasterComment)
class ClassTeacherMasterCommentAdmin(SchoolScopedAdminMixin, ModelAdmin):
    list_display = ['grade', 'stream', 'exam_type', 'year', 'term']
    list_filter = ['grade', 'stream', 'exam_type', 'year', 'term']


# ==================================================================================
# 🎨 UNIFIED TEACHER ADMIN - Fixed for proper admin URL routing
# ==================================================================================
@admin.register(Teacher)
class TeacherAdmin(SchoolScopedAdminMixin, ModelAdmin):
    # ============ LIST DISPLAY ============
    list_display = [
        'get_full_title', 'tsc_number', 'email', 'phone_number', 
        'subjects_taught', 'assigned_task', 'is_active_display', 'created_at'
    ]

    # ============ FILTERING & SEARCH ============
    list_filter = [
        'is_active', 'assigned_task', 'subjects_taught', 'created_at',
        ('user', admin.RelatedOnlyFieldListFilter),
    ]
    search_fields = [
        'user__first_name', 'user__last_name', 'user__username', 
        'tsc_number', 'email', 'subjects_taught', 'phone_number', 'classes'
    ]

    # ============ FIELDSETS - GROUPED SECTIONS ============
    fieldsets = (
        ('🔑 User Account Authentication', {
            'fields': ('user',),
            'description': 'Link this teacher profile to a system user account for login access.',
            'classes': ('collapse',)
        }),
        ('👤 Personal Information', {
            'fields': ('title', 'tsc_number', 'phone_number', 'email'),
            'description': "Teacher's identity and contact details.",
        }),
        ('📚 Teaching Assignment', {
            'fields': ('assigned_task', 'subjects_taught', 'classes'),
            'description': 'Define what and where this teacher teaches.',
        }),
        ('✅ Status & Activity', {
            'fields': ('is_active',),
            'description': 'Mark teacher as active or inactive.',
        }),
        ('📅 System Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
            'description': 'Automatically tracked timestamps.',
        }),
    )
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['user__first_name', 'user__last_name']
    actions = ['make_active', 'make_inactive', 'export_teacher_list']

    # ============ CUSTOM DISPLAY METHODS ============
    def get_full_title(self, obj):
        return obj.get_full_title()
    get_full_title.short_description = 'Teacher Name'
    get_full_title.admin_order_field = 'user__first_name'

    def is_active_display(self, obj):
        if obj.is_active:
            return mark_safe(
                '<span style="background-color: #22c55e; color: white; padding: 3px 8px; '
                'border-radius: 3px; font-weight: bold; font-size: 11px;">✓ ACTIVE</span>'
            )
        return mark_safe(
            '<span style="background-color: #ef4444; color: white; padding: 3px 8px; '
            'border-radius: 3px; font-weight: bold; font-size: 11px;">✗ INACTIVE</span>'
        )
    is_active_display.short_description = 'Status'
    is_active_display.admin_order_field = 'is_active'

    # ============ CUSTOM ACTIONS ============
    def make_active(self, request, queryset):
        updated = queryset.update(is_active=True)
        self.message_user(request, f'✓ {updated} teacher(s) marked as ACTIVE.')
    make_active.short_description = "✓ Mark selected teachers as ACTIVE"

    def make_inactive(self, request, queryset):
        updated = queryset.update(is_active=False)
        self.message_user(request, f'✗ {updated} teacher(s) marked as INACTIVE.')
    make_inactive.short_description = "✗ Mark selected teachers as INACTIVE"

    def export_teacher_list(self, request, queryset):
        self.message_user(request, f'📋 Export feature ready for {queryset.count()} teacher(s).')
    export_teacher_list.short_description = "📋 Export selected teachers"

    # ============ FORM CUSTOMIZATION ============
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')

    def formfield_for_foreignkey(self, db_field, request=None, **kwargs):
        if db_field.name == 'user':
            kwargs['queryset'] = User.objects.all().order_by('username')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def log_addition(self, request, object, message): pass
    def log_change(self, request, object, message): pass
    def log_deletion(self, request, object, message): pass


# -------------------- Subject Assignment Admin --------------------
@admin.register(SubjectAssignment)
class SubjectAssignmentAdmin(SchoolScopedAdminMixin, ModelAdmin):
    list_display = ['__str__']
    
    def log_addition(self, request, object, message): pass
    def log_change(self, request, object, message): pass
    def log_deletion(self, request, object, object_repr): pass


# ==============================================================================
# 🛡️ RE-REGISTER USER ADMIN (After unregistering default)
# ==============================================================================
@admin.register(User)
class CustomUserAdmin(UserAdmin, ModelAdmin):
    def log_addition(self, request, object, message): pass
    def log_change(self, request, object, message): pass
    def log_deletion(self, request, object, object_repr): pass   

#-------------------------SCHOOLS------------------------------#
@admin.register(School)
class SchoolAdmin(ModelAdmin):
    list_display = ['get_school_badge', 'code', 'tier_display', 'status_badge', 'get_validity_display', 'email', 'created_on']
    list_filter = ['status', 'tier', 'created_on']
    search_fields = ['name', 'code', 'email', 'phone_number']
    ordering = ['-created_on']
    
    # ✓ FIXED: Using mark_safe for static HTML content
    def status_badge(self, obj):
        if obj.status == 'active':
            return mark_safe('<span style="background: #22c55e; color: #fff; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 11px; text-transform: uppercase;">✓ Active</span>')
        elif obj.status == 'trial':
            return mark_safe('<span style="background: #eab308; color: #fff; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 11px; text-transform: uppercase;">⚠ Trial</span>')
        return mark_safe('<span style="background: #ef4444; color: #fff; padding: 4px 10px; border-radius: 12px; font-weight: 600; font-size: 11px; text-transform: uppercase;">✗ Suspended</span>')
    status_badge.short_description = "Status"

    # ✓ FIXED: Passed the variables to format_html correctly
    def tier_display(self, obj):
        colors = {'Basic': '#64748b', 'Premium': '#3b82f6', 'Enterprise': '#a855f7'}
        color = colors.get(obj.tier, '#000')
        return format_html('<span style="border: 1px solid {}; color: {}; padding: 2px 8px; border-radius: 6px; font-weight: 500; font-size: 11px;">{}</span>', color, color, obj.tier)
    tier_display.short_description = "Service Plan"

    # ✓ FIXED: Passed the object name to format_html correctly
    def get_school_badge(self, obj):
        return format_html('<strong style="color: #1e1b4b; font-size: 13px;">{}</strong>', obj.name)
    get_school_badge.short_description = "School Name"

    # ✓ FIXED: Passed the expiry date safely to format_html
    def get_validity_display(self, obj):
        if not obj.paid_until:
            return "No Limit"
        if obj.paid_until < now().date():
            return format_html('<span style="color: #ef4444; font-weight: 500;">Expired ({})</span>', obj.paid_until)
        return format_html('<span style="color: #059669; font-weight: 500;">Valid to {}</span>', obj.paid_until)
    get_validity_display.short_description = "Subscription Expiry"
    
from .models import SystemBroadcast

@admin.register(SystemBroadcast)
class SystemBroadcastAdmin(ModelAdmin):
    list_display = ['title', 'target_audience', 'is_active', 'created_at']
    list_filter = ['is_active', 'target_audience']
    search_fields = ['title', 'message']


@admin.register(SecurityAuditLog)
class SecurityAuditLogAdmin(ModelAdmin):
    list_display = [
        'timestamp',
        'action',
        'target_model',
        'target_id',
        'actor_id_snapshot',
        'client_ip',
        'school_id_snapshot',
    ]
    list_filter = ['action', 'target_model', 'timestamp']
    search_fields = ['target_id', 'target_model', 'actor_id_snapshot', 'client_ip', 'record_hash']
    readonly_fields = [
        'timestamp',
        'actor',
        'actor_id_snapshot',
        'client_ip',
        'action',
        'target_model',
        'target_id',
        'target_fields',
        'old_values',
        'new_values',
        'school_id_snapshot',
        'record_hash',
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

