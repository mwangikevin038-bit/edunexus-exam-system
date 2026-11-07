"""
Exam-space simulation suite.

Runs against the isolated Django TEST database (test_*) — the real
school_exam_db is never opened for writing by these tests.
"""
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import (
    Exam,
    ExamResultSnapshot,
    ExamSummary,
    Grade,
    Guardian,
    Mark,
    MarkSubmission,
    School,
    Student,
    Subject,
    Teacher,
)
from .views.constants import normalize_exam_display_name

User = get_user_model()


def _soft(name):
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


# CloseOldConnectionsMiddleware calls close_old_connections() after every
# request.  Django's close_if_unusable_or_obsolete() closes the connection
# whenever get_autocommit() != settings_dict["AUTOCOMMIT"] — which is ALWAYS
# true inside a TestCase (the test runs in an atomic block, so autocommit is
# False while AUTOCOMMIT is True).  The middleware therefore kills the shared
# connection at the end of every request and every later ORM call raises
# InterfaceError: "connection already closed".
#
# In production ATOMIC_REQUESTS=False, so autocommit is already True at
# request end and the middleware is a no-op.  It has zero effect on any
# response, so disabling it here does not reduce what these tests cover.
_TEST_MIDDLEWARE = [
    m for m in settings.MIDDLEWARE
    if "CloseOldConnectionsMiddleware" not in m
]


@override_settings(
    AXES_ENABLED=False,
    MIDDLEWARE=_TEST_MIDDLEWARE,
    SESSION_ENGINE="django.contrib.sessions.backends.cache",
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
        "csv_upload": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
        "pdf_generation": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    },
)
class ExamSpaceBase(TestCase):
    """Shared fixture: a school with all 9 grades and a school admin."""

    @classmethod
    def setUpTestData(cls):
        cls.school = School.objects.create(name="Sim School", code="sim")
        specs = (
            ("Grade 1", "PRIMARY", "LOWER", 1),
            ("Grade 2", "PRIMARY", "LOWER", 2),
            ("Grade 3", "PRIMARY", "LOWER", 3),
            ("Grade 4", "PRIMARY", "UPPER", 4),
            ("Grade 5", "PRIMARY", "UPPER", 5),
            ("Grade 6", "PRIMARY", "UPPER", 6),
            ("Grade 7", "JSS", None, 7),
            ("Grade 8", "JSS", None, 8),
            ("Grade 9", "JSS", None, 9),
        )
        cls.grades = {}
        for name, sec, sub, order in specs:
            cls.grades[name] = Grade.objects.create(
                school=cls.school, name=name, school_section=sec,
                sub_section=sub, order=order,
            )

        cls.admin_user = User.objects.create_user(
            username="sim_admin", password="x-pass-12345",
            first_name="Sim", last_name="Admin",
        )
        from students.models import SchoolAdmin
        SchoolAdmin.objects.create(user=cls.admin_user, school=cls.school)

        cls.subject = Subject.objects.create(
            school=cls.school, name="Mathematics", code="121",
        )
        # tsc_number and phone_number are unique per school, so only one
        # Teacher can exist with blank values — reuse a single one everywhere.
        teacher_user = User.objects.create_user(
            username="sim_teacher", password="x-pass-12345",
            first_name="Sim", last_name="Teacher",
        )
        cls.teacher = Teacher.objects.create(
            school=cls.school, user=teacher_user,
            school_section="PRIMARY", tsc_number="TSC-SIM-1",
            phone_number="0711000001",
        )
        guardian_user = User.objects.create_user(
            username="sim_guardian", password="x-pass-12345",
            first_name="Sim", last_name="Guardian",
        )
        cls.guardian = Guardian.objects.create(
            school=cls.school, user=guardian_user,
            name="Sim Guardian", phone="0711000002",
        )

    def setUp(self):
        self.client.force_login(self.admin_user)

    # ── helpers ────────────────────────────────────────────────────────
    def create(self, name, term="Term 3", year=2026, classes=(),
               status="active", follow=False):
        return self.client.post(
            reverse("manage_exams"),
            {
                "action_type": "create_exam",
                "exam_name": name,
                "term": term,
                "year": year,
                "status": status,
                "selected_classes": list(classes),
            },
            follow=follow,
        )

    @property
    def exams(self):
        return Exam.all_objects.filter(school=self.school)

    def messages_of(self, response):
        if not response.context:
            return []
        return [str(m) for m in response.context["messages"]]

    def make_submission(self, class_name, exam_name, term="Term 3", year=2026,
                        status="submitted", sub_section=None, subject=None):
        """Create a Mark + MarkSubmission pair.

        Both must exist: Manage Exams drops submissions that have no
        matching Mark (orphan guard), so creating a MarkSubmission alone
        would simply never appear.
        """
        sec = "JSS" if class_name in ("Grade 7", "Grade 8", "Grade 9") else "PRIMARY"
        if sub_section is None:
            if class_name in ("Grade 1", "Grade 2", "Grade 3"):
                sub_section = "LOWER"
            elif class_name in ("Grade 4", "Grade 5", "Grade 6"):
                sub_section = "UPPER"
        subject = subject or self.subject
        n = Student.all_objects.count() + 1
        student = Student.objects.create(
            school=self.school, name=f"Sim Learner {n}",
            admission_no=f"SIM-{n}", guardian=self.guardian,
            school_section=sec, sub_section=sub_section,
            class_name=class_name, stream="Main",
        )
        Mark.objects.create(
            school=self.school, student=student, subject=subject,
            score=50, raw_score=25, maximum_marks=50,
            term=term, year=year, exam_type=exam_name,
            school_section=sec, sub_section=sub_section,
        )
        return MarkSubmission.objects.create(
            school=self.school, teacher=self.teacher, subject=subject,
            school_section=sec, sub_section=sub_section,
            class_name=class_name, stream="Main",
            exam_name=exam_name, term=term, year=year, status=status,
        )

    def manage_rows(self, year=2026):
        """Return [(section_label, exam_name, exam_status, [classes...],
        {class: per_class_status}), ...]."""
        resp = self.client.get(f"{reverse('manage_exams')}?tab=manage&year={year}")
        self.assertEqual(resp.status_code, 200)
        out = []
        for term_entry in resp.context["exam_list_by_term"].values():
            for e in term_entry["exams"]:
                out.append((
                    e.get("section_label"),
                    e["name"],
                    e["status"],
                    [c["class_name"] for c in e["classes"]],
                    {c["class_name"]: c["status"] for c in e["classes"]},
                ))
        return out

    def row_named(self, name, section=None):
        want = normalize_exam_display_name(name)
        hits = [r for r in self.manage_rows()
                if r[1] == want and (section is None or r[0] == section)]
        self.assertEqual(len(hits), 1, f"expected 1 row for {want!r}/{section}, got {hits}")
        return hits[0]


# ══════════════════════════════════════════════════════════════════════
# A. Class independence on create
# ══════════════════════════════════════════════════════════════════════
class TestCreateIndependence(ExamSpaceBase):
    def test_grade6_only_creates_scoped_exam(self):
        self.create("Science Test", classes=["Grade 6"])
        e = self.exams.get(name="Science Test")
        self.assertEqual(e.classes, ["Grade 6"])
        self.assertEqual((e.school_section, e.sub_section), ("PRIMARY", "UPPER"))

    def test_grade6_only_shows_only_grade6(self):
        """Sibling-grade submissions must not leak into a scoped exam."""
        for g in ("Grade 4", "Grade 5", "Grade 6"):
            self.make_submission(g, "Opener Assessment")
        self.create("Opener Assessment", classes=["Grade 6"])
        rows = self.manage_rows()
        opener = [r for r in rows if r[1] == "Opener Assessment" and r[0] == "Upper Primary"]
        self.assertEqual(len(opener), 1, opener)
        self.assertEqual(opener[0][3], ["Grade 6"], opener)

    def test_select_all_creates_one_exam_per_section(self):
        self.create("Whole School Test",
                    classes=[f"Grade {i}" for i in range(1, 10)])
        qs = self.exams.filter(name="Whole School Test")
        self.assertEqual(qs.count(), 3)
        self.assertEqual(
            {(e.school_section, e.sub_section) for e in qs},
            {("PRIMARY", "LOWER"), ("PRIMARY", "UPPER"), ("JSS", None)},
        )
        by_key = {(e.school_section, e.sub_section): e.classes for e in qs}
        self.assertEqual(by_key[("PRIMARY", "LOWER")], ["Grade 1", "Grade 2", "Grade 3"])
        self.assertEqual(by_key[("PRIMARY", "UPPER")], ["Grade 4", "Grade 5", "Grade 6"])
        self.assertEqual(by_key[("JSS", None)], ["Grade 7", "Grade 8", "Grade 9"])

    def test_grades_ordered_numerically(self):
        self.create("Ordered", classes=["Grade 9", "Grade 7", "Grade 8"])
        self.assertEqual(self.exams.get(name="Ordered").classes,
                         ["Grade 7", "Grade 8", "Grade 9"])


# ══════════════════════════════════════════════════════════════════════
# B. Collision blocking (was: silent merge / HTTP 500)
# ══════════════════════════════════════════════════════════════════════
class TestCollisionBlocking(ExamSpaceBase):
    def test_same_name_same_section_different_classes_blocked(self):
        self.create("Opener", classes=["Grade 4", "Grade 5"], follow=True)
        resp = self.create("Opener", classes=["Grade 6"], follow=True)
        self.assertEqual(self.exams.filter(name="Opener").count(), 1)
        self.assertEqual(self.exams.get(name="Opener").classes, ["Grade 4", "Grade 5"])
        msgs = " ".join(self.messages_of(resp))
        self.assertIn("already exists", msgs)
        self.assertIn("Grade 4, Grade 5", msgs)

    def test_same_name_same_classes_is_info_not_error(self):
        # follow=True so the first create's flash message is consumed and
        # does not leak into the assertions on the second response.
        self.create("Opener", classes=["Grade 6"], follow=True)
        resp = self.create("Opener", classes=["Grade 6"], follow=True)
        self.assertEqual(self.exams.filter(name="Opener").count(), 1)
        self.assertEqual(self.exams.get(name="Opener").classes, ["Grade 6"])
        self.assertIn("already exist", " ".join(self.messages_of(resp)))

    def test_same_name_other_section_allowed(self):
        """Independence: UPPER reuse must not touch LOWER."""
        self.create("Opener", classes=["Grade 1", "Grade 2"])
        self.create("Opener", classes=["Grade 6"])
        qs = self.exams.filter(name="Opener")
        self.assertEqual(qs.count(), 2)
        self.assertEqual(
            {tuple(sorted(e.classes)) for e in qs},
            {("Grade 1", "Grade 2"), ("Grade 6",)},
        )

    def test_closed_twin_blocks_instead_of_500(self):
        """Regression: this used to raise IntegrityError (HTTP 500)."""
        self.create("Opener", classes=["Grade 4", "Grade 5", "Grade 6"], follow=True)
        self.client.post(reverse("manage_exams"), {
            "action_type": "toggle_status",
            "exam_id": self.exams.get(name="Opener").pk,
            "year": 2026,
        })
        self.assertEqual(self.exams.get(name="Opener").status, "closed")
        resp = self.create("Opener", classes=["Grade 6"], follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.exams.filter(name="Opener").count(), 1)
        self.assertIn("already exists", " ".join(self.messages_of(resp)))

    def test_softdeleted_twin_points_to_deleted_tab(self):
        self.create("Opener", classes=["Grade 4"])
        self.client.post(reverse("manage_exams"), {
            "action_type": "delete_exam",
            "exam_id": self.exams.get(name="Opener").pk,
            "year": 2026,
        })
        self.assertTrue(self.exams.get(name="Opener").is_deleted)
        resp = self.create("Opener", classes=["Grade 6"], follow=True)
        msgs = " ".join(self.messages_of(resp))
        self.assertIn("Deleted Assessments", msgs)
        self.assertEqual(self.exams.filter(name="Opener", is_deleted=True).count(), 1)
        self.assertEqual(self.exams.filter(name="Opener", is_deleted=False).count(), 0)

    def test_blocked_create_preserves_typed_values(self):
        self.create("Opener", classes=["Grade 4"])
        self.create("Opener", classes=["Grade 6"])
        resp = self.client.get(f"{reverse('manage_exams')}?tab=create")
        prefill = resp.context.get("create_prefill") or self.client.session.get("create_prefill")
        self.assertEqual((prefill or {}).get("name"), "Opener")
        self.assertEqual((prefill or {}).get("classes"), ["Grade 6"])


# ══════════════════════════════════════════════════════════════════════
# C. Display: one row per section, no cross-section merge
# ══════════════════════════════════════════════════════════════════════
class TestDisplayScoping(ExamSpaceBase):
    def test_no_cross_section_merge(self):
        self.create("Opener", classes=["Grade 1", "Grade 2"])
        self.create("Opener", classes=["Grade 6"])
        self.create("Opener", classes=["Grade 8"])
        rows = self.manage_rows()
        # Manage Exams displays the *normalized* name.
        opener = [r for r in rows if r[1] == normalize_exam_display_name("Opener")]
        self.assertEqual(len(opener), 3, opener)
        self.assertEqual({r[0] for r in opener},
                         {"Lower Primary", "Upper Primary", "Junior Secondary"})
        self.assertEqual(
            {tuple(sorted(r[3])) for r in opener},
            {("Grade 1", "Grade 2"), ("Grade 6",), ("Grade 8",)},
        )

    def test_submissions_outside_scope_do_not_appear(self):
        self.create("Scoped", classes=["Grade 1", "Grade 2"])
        for g in ("Grade 1", "Grade 2", "Grade 3"):
            self.make_submission(g, "Scoped")
        rows = self.manage_rows()
        scoped = [r for r in rows if r[1] == "Scoped"]
        self.assertEqual(scoped[0][3], ["Grade 1", "Grade 2"], scoped)

    def test_legacy_empty_classes_shows_whole_section(self):
        Exam.objects.create(
            school=self.school, name="Legacy", term="Term 3", year=2026,
            school_section="PRIMARY", sub_section="UPPER",
            status="active", classes=[],
        )
        rows = self.manage_rows()
        legacy = [r for r in rows if r[1] == "Legacy"]
        self.assertEqual(legacy[0][3], ["Grade 4", "Grade 5", "Grade 6"])

    def test_status_reflects_submissions(self):
        self.create("Statused", classes=["Grade 6"])
        row = self.row_named("Statused", "Upper Primary")
        self.assertEqual(row[3], ["Grade 6"])
        self.assertEqual(row[2], "active", "exam-level status is the DB field")
        self.assertEqual(row[4]["Grade 6"], "Not Started")

        # One submission per (class, exam, subject) — a second subject is what
        # actually produces a partial-progress state.
        science = Subject.objects.create(
            school=self.school, name="Science", code="123",
        )
        self.make_submission("Grade 6", "Statused", status="submitted")
        row = self.row_named("Statused", "Upper Primary")
        self.assertEqual(row[4]["Grade 6"], "Pending Publishing")

        self.make_submission("Grade 6", "Statused", status="published", subject=science)
        row = self.row_named("Statused", "Upper Primary")
        self.assertEqual(row[4]["Grade 6"], "Pending Publishing",
                         "1 published + 1 submitted => not all published")

        # NB: MarkSubmission.objects is SchoolScopedManager and yields an
        # empty qs when no tenant is bound — use all_objects for test writes.
        MarkSubmission.all_objects.filter(exam_name="Statused").update(status="published")
        row = self.row_named("Statused", "Upper Primary")
        self.assertEqual(row[4]["Grade 6"], "Published")

    def test_section_label_values(self):
        for name, classes, expect in (
            ("A", ["Grade 2"], "Lower Primary"),
            ("B", ["Grade 5"], "Upper Primary"),
            ("C", ["Grade 8"], "Junior Secondary"),
        ):
            self.create(name, classes=classes)
        rows = self.manage_rows()
        labels = {r[1]: r[0] for r in rows}
        self.assertEqual(labels["A"], "Lower Primary")
        self.assertEqual(labels["B"], "Upper Primary")
        self.assertEqual(labels["C"], "Junior Secondary")


# ══════════════════════════════════════════════════════════════════════
# D. Action endpoints stay scoped
# ══════════════════════════════════════════════════════════════════════
class TestActionScoping(ExamSpaceBase):
    def test_toggle_status_affects_only_its_section(self):
        self.create("Toggle", classes=["Grade 1", "Grade 2"])
        self.create("Toggle", classes=["Grade 6"])
        lower = self.exams.get(name="Toggle", sub_section="LOWER")
        upper = self.exams.get(name="Toggle", sub_section="UPPER")
        self.client.post(reverse("manage_exams"), {
            "action_type": "toggle_status", "exam_id": lower.pk, "year": 2026,
        })
        lower.refresh_from_db(); upper.refresh_from_db()
        self.assertEqual(lower.status, "closed")
        self.assertEqual(upper.status, "active")

    def test_per_class_delete_removes_only_that_class(self):
        self.create("DelTest", classes=["Grade 4", "Grade 5", "Grade 6"])
        for g in ("Grade 4", "Grade 5"):
            self.make_submission(g, "DelTest")
        exam = self.exams.get(name="DelTest")
        self.client.post(reverse("manage_exams"), {
            "action_type": "delete_exam", "exam_id": exam.pk,
            "class_name": "Grade 4", "year": 2026,
        })
        self.assertEqual(
            sorted(MarkSubmission.all_objects.filter(
                school=self.school, exam_name="DelTest").values_list("class_name", flat=True)),
            ["Grade 5"],
        )
        exam.refresh_from_db()
        self.assertFalse(exam.is_deleted, "per-class delete must not kill the whole exam")

    def test_per_class_withdraw_scopes_to_class(self):
        self.create("WTest", classes=["Grade 4", "Grade 5"])
        self.make_submission("Grade 4", "WTest", status="published")
        self.make_submission("Grade 5", "WTest", status="published")
        exam = self.exams.get(name="WTest")
        self.client.post(reverse("manage_exams"), {
            "action_type": "withdraw_results",
            "withdraw_exam_id": exam.pk,
            "withdraw_class_name": "Grade 4",
            "year": 2026,
        })
        g4 = MarkSubmission.all_objects.get(school=self.school, exam_name="WTest", class_name="Grade 4")
        g5 = MarkSubmission.all_objects.get(school=self.school, exam_name="WTest", class_name="Grade 5")
        self.assertEqual(g4.status, "returned")
        self.assertEqual(g5.status, "published")

    def test_withdraw_on_closed_exam_refused(self):
        self.create("CW", classes=["Grade 6"])
        self.make_submission("Grade 6", "CW", status="published")
        exam = self.exams.get(name="CW")
        self.client.post(reverse("manage_exams"), {
            "action_type": "toggle_status", "exam_id": exam.pk, "year": 2026,
        })
        self.client.post(reverse("manage_exams"), {
            "action_type": "withdraw_results",
            "withdraw_exam_id": exam.pk, "withdraw_class_name": "Grade 6", "year": 2026,
        })
        sub = MarkSubmission.all_objects.get(school=self.school, exam_name="CW")
        self.assertEqual(sub.status, "published", "closed exam must refuse withdraw")

    def test_delete_whole_exam_requires_open_status(self):
        self.create("Whole", classes=["Grade 6"])
        exam = self.exams.get(name="Whole")
        self.client.post(reverse("manage_exams"), {
            "action_type": "toggle_status", "exam_id": exam.pk, "year": 2026,
        })
        self.client.post(reverse("manage_exams"), {
            "action_type": "delete_exam", "exam_id": exam.pk, "year": 2026,
        })
        exam.refresh_from_db()
        self.assertFalse(exam.is_deleted, "closed exam must not be deletable")

    def test_forged_exam_id_is_rejected(self):
        for payload in (
            {"action_type": "toggle_status", "exam_id": "abc", "year": 2026},
            {"action_type": "delete_exam", "exam_id": "-5", "year": 2026},
            {"action_type": "toggle_status", "year": 2026},
        ):
            resp = self.client.post(reverse("manage_exams"), payload)
            self.assertEqual(resp.status_code, 302)


# ══════════════════════════════════════════════════════════════════════
# E. Create-form validation
# ══════════════════════════════════════════════════════════════════════
class TestCreateValidation(ExamSpaceBase):
    def _assert_rejected(self, **kw):
        before = self.exams.count()
        resp = self.create(follow=True, **kw)
        self.assertEqual(self.exams.count(), before,
                         f"expected rejection for {kw}")
        return " ".join(self.messages_of(resp))

    def test_no_classes_rejected(self):
        self.assertIn("at least one class", self._assert_rejected(name="X", classes=[]))

    def test_missing_name_rejected(self):
        self.assertIn("assessment name", self._assert_rejected(name="", classes=["Grade 6"]).lower())

    def test_missing_term_rejected(self):
        before = self.exams.count()
        resp = self.client.post(reverse("manage_exams"), {
            "action_type": "create_exam", "exam_name": "X", "term": "",
            "year": 2026, "status": "active", "selected_classes": ["Grade 6"],
        }, follow=True)
        self.assertEqual(self.exams.count(), before)
        self.assertIn("Invalid term", " ".join(self.messages_of(resp)))

    def test_bad_status_rejected(self):
        before = self.exams.count()
        self.client.post(reverse("manage_exams"), {
            "action_type": "create_exam", "exam_name": "X", "term": "Term 3",
            "year": 2026, "status": "zombie", "selected_classes": ["Grade 6"],
        }, follow=True)
        self.assertEqual(self.exams.count(), before)

    def test_overlong_name_rejected(self):
        self.assertIn("too long", self._assert_rejected(name="N" * 51, classes=["Grade 6"]))

    def test_year_out_of_range_rejected(self):
        before = self.exams.count()
        self.client.post(reverse("manage_exams"), {
            "action_type": "create_exam", "exam_name": "X", "term": "Term 3",
            "year": 3000, "status": "active", "selected_classes": ["Grade 6"],
        }, follow=True)
        self.assertEqual(self.exams.count(), before)

    def test_unknown_grade_rejected(self):
        self.assertIn("Unknown class", self._assert_rejected(name="X", classes=["Grade 99"]))

    def test_name_is_whitespace_collapsed(self):
        self.create("  My    Exam  ", classes=["Grade 6"])
        self.assertEqual(self.exams.get().name, "My Exam")


# ══════════════════════════════════════════════════════════════════════
# F. Downstream scoping surfaces
# ══════════════════════════════════════════════════════════════════════
class TestDownstreamScoping(ExamSpaceBase):
    def test_exam_covers_class_helper(self):
        from students.views.helpers import exam_covers_class
        scoped = Exam(name="S", classes=["Grade 6"])
        legacy = Exam(name="L", classes=[])
        self.assertTrue(exam_covers_class(scoped, "Grade 6"))
        self.assertFalse(exam_covers_class(scoped, "Grade 4"))
        self.assertTrue(exam_covers_class(legacy, "Grade 4"))
        self.assertFalse(exam_covers_class(scoped, ""))

    def test_exam_dropdown_scopes_by_class(self):
        from students.views.helpers import filter_exams_by_classes
        Exam.objects.create(school=self.school, name="Only6", term="Term 3", year=2026,
                            school_section="PRIMARY", sub_section="UPPER",
                            status="active", classes=["Grade 6"])
        # Exam.objects is SchoolScopedManager (empty when no tenant bound);
        # production builds this qs inside a request where the tenant IS bound.
        qs = Exam.all_objects.filter(school=self.school)
        self.assertEqual(qs.count(), 1)
        self.assertEqual([e.name for e in filter_exams_by_classes(qs, "Grade 6")], ["Only6"])
        self.assertEqual(list(filter_exams_by_classes(qs, "Grade 4")), [])

    def test_normalized_name_grouping(self):
        self.assertEqual(normalize_exam_display_name("Opener Exam"),
                         normalize_exam_display_name("Opener Assessment"))
        self.assertNotEqual(normalize_exam_display_name("Opener Assessment"),
                            normalize_exam_display_name("Mid Term Assessment"))


# ══════════════════════════════════════════════════════════════════════
# G. Dashboard grade-performance cards
# ══════════════════════════════════════════════════════════════════════
class TestDashboardPerformanceCards(ExamSpaceBase):
    """The Academic Performance cards must resolve the right exam per grade,
    name the exam most grades are on, and reconcile headcounts with means."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # Three different Exam rows share the name 'Opener Exam' in Term 3.
        cls.opener_lower = Exam.objects.create(
            school=cls.school, name="Opener Exam", term="Term 3", year=2026,
            school_section="PRIMARY", sub_section="LOWER", status="closed",
            classes=[],
        )
        cls.opener_upper = Exam.objects.create(
            school=cls.school, name="Opener Exam", term="Term 3", year=2026,
            school_section="PRIMARY", sub_section="UPPER", status="closed",
            classes=[],
        )
        cls.opener_jss = Exam.objects.create(
            school=cls.school, name="Opener Exam", term="Term 3", year=2026,
            school_section="JSS", sub_section=None, status="closed",
            classes=[],
        )
        cls.eot_lower_g1 = Exam.objects.create(
            school=cls.school, name="End of Term Assessment", term="Term 3",
            year=2026, school_section="PRIMARY", sub_section="LOWER",
            status="closed", classes=["Grade 1"],
        )

    def _publish(self, class_name, exam_name, present=2, absent=0, score=50,
                 points=2, term="Term 3", year=2026):
        """One submission plus marks for `present` students and `absent` more."""
        sec = "JSS" if class_name in ("Grade 7", "Grade 8", "Grade 9") else "PRIMARY"
        if class_name in ("Grade 1", "Grade 2", "Grade 3"):
            sub = "LOWER"
        elif class_name in ("Grade 4", "Grade 5", "Grade 6"):
            sub = "UPPER"
        else:
            sub = None
        sub_obj = MarkSubmission.objects.create(
            school=self.school, teacher=self.teacher, subject=self.subject,
            school_section=sec, sub_section=sub, class_name=class_name,
            stream="Main", exam_name=exam_name, term=term, year=year,
            status="published",
        )
        n0 = Student.all_objects.count()
        for i in range(present + absent):
            st = Student.objects.create(
                school=self.school, name="Sim Learner D%d" % (n0 + i),
                admission_no="SIM-D%d" % (n0 + i), guardian=self.guardian,
                school_section=sec, sub_section=sub, class_name=class_name,
                stream="Main",
            )
            mark = Mark.objects.create(
                school=self.school, student=st, subject=self.subject,
                score=score, raw_score=score, maximum_marks=100,
                is_absent=(i >= present),
                term=term, year=year, exam_type=exam_name,
                school_section=sec, sub_section=sub,
            )
            # Mark.save() recomputes `points` from the school's GradingScale.
            # The fixture school has none, so save() forces 0 — pin the value
            # the card assertions expect without re-running save().
            Mark.all_objects.filter(pk=mark.pk).update(points=points)
        return sub_obj

    def _keys(self, grades):
        from .views.dashboard import _latest_exam_keys_by_grade
        return _latest_exam_keys_by_grade(
            MarkSubmission.all_objects.filter(school=self.school),
            grades, self.school,
        )

    # -- section + classes resolution ----------------------------------
    def test_section_for_grade_mapping(self):
        from .views.dashboard import _section_for_grade
        self.assertEqual(_section_for_grade("Grade 1"), ("PRIMARY", "LOWER"))
        self.assertEqual(_section_for_grade("Grade 3"), ("PRIMARY", "LOWER"))
        self.assertEqual(_section_for_grade("Grade 4"), ("PRIMARY", "UPPER"))
        self.assertEqual(_section_for_grade("Grade 6"), ("PRIMARY", "UPPER"))
        self.assertEqual(_section_for_grade("Grade 7"), ("JSS", None))
        self.assertEqual(_section_for_grade("Grade 9"), ("JSS", None))

    def test_exam_id_for_picks_the_rows_own_section(self):
        """Three exams share a name; each grade must get ITS section's row."""
        from .views.dashboard import _exam_id_for
        key = ("Opener Exam", "Term 3", 2026)
        self.assertEqual(_exam_id_for(self.school, key, "Grade 1"), self.opener_lower.pk)
        self.assertEqual(_exam_id_for(self.school, key, "Grade 3"), self.opener_lower.pk)
        self.assertEqual(_exam_id_for(self.school, key, "Grade 4"), self.opener_upper.pk)
        self.assertEqual(_exam_id_for(self.school, key, "Grade 7"), self.opener_jss.pk)
        self.assertEqual(_exam_id_for(self.school, key, "Grade 9"), self.opener_jss.pk)

    def test_exam_id_for_skips_rows_that_do_not_cover_the_grade(self):
        """A same-named row scoped to another class must not be returned.

        The DB unique constraint on (school, name, term, year, section,
        sub_section) means at most one row exists per section, so the
        resolver must simply skip a row whose `classes` exclude the grade.
        """
        from .views.dashboard import _exam_id_for
        scoped = Exam.objects.create(
            school=self.school, name="Scoped", term="Term 3", year=2026,
            school_section="PRIMARY", sub_section="LOWER", status="closed",
            classes=["Grade 2"],
        )
        key = ("Scoped", "Term 3", 2026)
        self.assertEqual(_exam_id_for(self.school, key, "Grade 2"), scoped.pk)
        # Grade 3 is not in the row's classes → no resolvable exam.
        self.assertIsNone(_exam_id_for(self.school, key, "Grade 3"))

    def test_exam_covers_grade_rejects_other_sections(self):
        from .views.dashboard import _exam_covers_grade, _exam_id_for
        key = ("Opener Exam", "Term 3", 2026)
        self.assertTrue(_exam_covers_grade(self.school, key, "Grade 1"))
        self.assertTrue(_exam_covers_grade(self.school, key, "Grade 7"))
        # Every covered grade must resolve to an exam in its own section.
        for g in ("Grade 1", "Grade 3", "Grade 4", "Grade 7", "Grade 9"):
            eid = _exam_id_for(self.school, key, g)
            e = Exam.all_objects.get(pk=eid)
            self.assertEqual(e.school_section, ("JSS" if g in ("Grade 7", "Grade 9") else "PRIMARY"))

    # -- header + delta labelling -------------------------------------
    def test_header_names_the_exam_most_grades_are_on(self):
        from .views.dashboard import _header_exam_key
        self._publish("Grade 1", "End of Term Assessment")
        for g in ("Grade 2", "Grade 3", "Grade 7"):
            self._publish(g, "Opener Exam")
        keys = self._keys(["Grade 1", "Grade 2", "Grade 3", "Grade 7"])
        self.assertEqual(_header_exam_key(keys), ("Opener Exam", "Term 3", 2026))

    def test_header_falls_back_to_newer_on_a_tie(self):
        from .views.dashboard import _header_exam_key
        keys = {
            "Grade 1": [("Opener Exam", "Term 3", 2026)],
            "Grade 2": [("End of Term Assessment", "Term 3", 2026)],
        }
        self.assertEqual(_header_exam_key(keys), ("End of Term Assessment", "Term 3", 2026))
        self.assertEqual(_header_exam_key({}), None)

    def test_header_tie_breaks_toward_the_most_recently_published(self):
        """A tied header names whichever exam the admin published last."""
        from .views.dashboard import _header_exam_key
        opener = ("Opener Exam", "Term 3", 2026)
        eot = ("End of Term Assessment", "Term 3", 2026)
        keys = {"Grade 1": [opener], "Grade 2": [eot]}
        self.assertEqual(
            _header_exam_key(keys, {opener: 1000.0, eot: 2000.0}), eot)
        self.assertEqual(
            _header_exam_key(keys, {opener: 3000.0, eot: 2000.0}), opener)
        # No publish clock available -> academic order (End of Term ranks newer).
        self.assertEqual(_header_exam_key(keys), eot)

    # -- publish recency decides which exam a card shows ----------------
    def test_cards_pick_the_most_recently_published_exam(self):
        """The admin's LAST publish wins even though the academic calendar
        ranks End of Term above Opener by name."""
        from datetime import timedelta

        from django.utils import timezone

        self._publish("Grade 1", "End of Term Assessment")
        self._publish("Grade 1", "Opener Exam")
        now = timezone.now()
        MarkSubmission.all_objects.filter(
            school=self.school, class_name="Grade 1",
            exam_name="End of Term Assessment",
        ).update(published_at=now - timedelta(days=1))
        MarkSubmission.all_objects.filter(
            school=self.school, class_name="Grade 1",
            exam_name="Opener Exam",
        ).update(published_at=now)
        self.assertEqual(
            self._keys(["Grade 1"])["Grade 1"],
            [("Opener Exam", "Term 3", 2026),
             ("End of Term Assessment", "Term 3", 2026)],
        )

    def test_cards_fall_back_to_academic_order_without_publish_times(self):
        """Legacy rows published before published_at existed keep the old rank."""
        self._publish("Grade 1", "End of Term Assessment")
        self._publish("Grade 1", "Opener Exam")
        keys = self._keys(["Grade 1"])["Grade 1"]
        self.assertEqual(keys[0], ("End of Term Assessment", "Term 3", 2026))
        self.assertEqual(keys[1], ("Opener Exam", "Term 3", 2026))

    def test_dashboard_card_renders_the_last_published_exam(self):
        """End to end: the rendered card labels the exam published last."""
        from datetime import timedelta

        from django.utils import timezone

        self._publish("Grade 1", "End of Term Assessment")
        self._publish("Grade 1", "Opener Exam")
        now = timezone.now()
        MarkSubmission.all_objects.filter(
            school=self.school, class_name="Grade 1",
            exam_name="End of Term Assessment",
        ).update(published_at=now - timedelta(days=1))
        MarkSubmission.all_objects.filter(
            school=self.school, class_name="Grade 1",
            exam_name="Opener Exam",
        ).update(published_at=now)
        resp = self.client.get(reverse("school_admin_dashboard"))
        card = {c["label"]: c
                for c in resp.context["grade_performance_cards"]}["Grade 1"]
        self.assertEqual(card["exam_label"], "OPENER EXAM - (2026 TERM 3)")

    def test_latest_publish_ts_reports_the_last_publish_time(self):
        from django.utils import timezone

        from .views.dashboard import _latest_publish_ts
        sub = self._publish("Grade 2", "Opener Exam")
        MarkSubmission.all_objects.filter(pk=sub.pk).update(
            published_at=timezone.now())
        ts = _latest_publish_ts(
            MarkSubmission.all_objects.filter(school=self.school),
            [("Opener Exam", "Term 3", 2026), ("Never Shown", "Term 3", 2026)],
        )
        self.assertIn(("Opener Exam", "Term 3", 2026), ts)
        self.assertNotIn(("Never Shown", "Term 3", 2026), ts)
        self.assertGreater(ts[("Opener Exam", "Term 3", 2026)], 0)


    def test_delta_baseline_label(self):
        from .views.dashboard import _delta_baseline_label
        cur = ("Opener Exam", "Term 3", 2026)
        self.assertEqual(_delta_baseline_label(cur, ("KNEC", "Term 3", 2026)), "vs Knec")
        self.assertEqual(_delta_baseline_label(cur, ("End", "Term 2", 2026)),
                         "vs End 2026 Term 2")
        self.assertEqual(_delta_baseline_label(cur, None), "")

    # -- orphaned submissions must not surface an exam ------------------
    def test_latest_keys_drop_exams_not_covering_the_grade(self):
        """A published submission under an exam that never covered the class
        must not put that exam on the card."""
        self._publish("Grade 3", "End of Term Assessment")  # exam scoped to G1 only
        self.assertEqual(self._keys(["Grade 1", "Grade 3"]).get("Grade 3"), [])

    # -- headcount / mean reconciliation -------------------------------
    def test_enrolled_absent_and_mean_reconcile(self):
        self._publish("Grade 2", "Opener Exam", present=3, absent=2,
                      score=40, points=2)
        resp = self.client.get(reverse("school_admin_dashboard"))
        self.assertEqual(resp.status_code, 200)
        card = {c["label"]: c for c in resp.context["grade_performance_cards"]}["Grade 2"]
        self.assertEqual(card["enrolled_count"], 5)
        self.assertEqual(card["student_count"], 3)
        # enrolled = sat + absent, and the mean is over `sat` only.
        self.assertEqual(card["absent_count"], 2)
        self.assertEqual(card["enrolled_count"],
                         card["student_count"] + card["absent_count"])
        self.assertEqual(card["mean_score"], 40.0)
        self.assertEqual(card["mean_points"], 2.0)

    def test_mean_points_rounds_to_two_decimals(self):
        self._publish("Grade 2", "Opener Exam", present=1, score=41, points=2)
        from .views.dashboard import _stats_by_exam_and_grade, _published_tuples_by_exam
        qs = MarkSubmission.all_objects.filter(school=self.school)
        keys = [("Opener Exam", "Term 3", 2026)]
        tuples = _published_tuples_by_exam(qs, ["Grade 2"], keys)
        stats = _stats_by_exam_and_grade(Mark.all_objects.filter(school=self.school), tuples)
        self.assertEqual(stats[(keys[0], "Grade 2")]["mean_score"], 41.0)
        self.assertEqual(stats[(keys[0], "Grade 2")]["mean_points"], 2.0)

    def test_only_published_exams_reach_the_cards(self):
        """A draft submission must not become a card's current exam."""
        self._publish("Grade 5", "Opener Exam")
        sub = MarkSubmission.all_objects.filter(
            school=self.school, class_name="Grade 5").first()
        sub.status = "submitted"
        sub.save()
        # No published submission remains → the grade gets no exam key at all.
        self.assertFalse(self._keys(["Grade 5"]).get("Grade 5"))

    # -- integration ---------------------------------------------------
    def test_every_view_analysis_link_covers_its_grade(self):
        self._publish("Grade 1", "End of Term Assessment")
        self._publish("Grade 1", "Opener Exam")
        for g in ("Grade 2", "Grade 3", "Grade 7"):
            self._publish(g, "Opener Exam")
        from urllib.parse import unquote_plus

        from .views.dashboard import _section_for_grade
        resp = self.client.get(reverse("school_admin_dashboard"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # Django urlencode's the label, so the space arrives as %20 or +.
        links = re.findall(
            r"exam_id=(\d+)&class_name=(Grade(?:%20|\+| )\d+)", html)
        self.assertTrue(links, "expected View Analysis links")
        checked = set()
        for eid, enc in links:
            grade = unquote_plus(enc)
            if (eid, grade) in checked:
                continue
            checked.add((eid, grade))
            e = Exam.all_objects.get(pk=int(eid))
            sec, sub = _section_for_grade(grade)
            self.assertEqual(e.school_section, sec, "%s -> exam %s" % (grade, eid))
            self.assertEqual(e.sub_section, sub, "%s -> exam %s" % (grade, eid))
            self.assertTrue(not e.classes or grade in e.classes,
                            "%s not in exam %s classes %s" % (grade, eid, e.classes))

    def test_desktop_and_mobile_both_render_the_new_design(self):
        # A second Grade 2 exam + an absentee so the delta caption and the
        # "N absent" note are both triggered on the card.
        Exam.objects.create(
            school=self.school, name="Mid Term", term="Term 3", year=2026,
            school_section="PRIMARY", sub_section="LOWER", status="closed",
            classes=[],
        )
        self._publish("Grade 2", "Opener Exam", present=2, score=40)
        self._publish("Grade 2", "Mid Term", present=2, absent=1, score=44)
        resp = self.client.get(reverse("school_admin_dashboard"))
        html = resp.content.decode()
        self.assertIn("performance-analysis-grid", html)  # desktop block
        self.assertIn("admin-ios", html)                  # mobile shell
        self.assertNotIn("floatformat:4", html)           # stale precision gone
        self.assertIn("2.00", html)                       # mean points, 2 dp
        # One shared partial feeds both layouts: 9 grades x 2 layouts, and
        # the captions/notes render once per layout for the one graded card.
        self.assertEqual(html.count('class="analysis-card"'), 18)
        self.assertEqual(html.count('class="dev-baseline"'), 2)
        self.assertEqual(html.count('class="perf-absent"'), 2)
        self.assertEqual(html.count('class="btn-view-analysis"'), 2)
        # The mobile-only markup is retired — cards can no longer drift.
        self.assertNotIn('class="dash-perf-card"', html)
        self.assertNotIn("dash-perf-cell-value", html)


# ══════════════════════════════════════════════════════════════════════
# H. Manage Exams -> Analyse Exam: default exam, grade scoping, numbers
# ══════════════════════════════════════════════════════════════════════
class TestAnalyseExamDefaults(ExamSpaceBase):
    """The bare analyse URL opens the LATEST PUBLISHED exam (by
    MarkSubmission.published_at) — never the earliest term of the newest
    academic year — and a mismatched exam/grade URL lands on a covering
    exam instead of empty data."""

    def _exam(self, name, term, year=2026, section="JSS", classes=()):
        return Exam.objects.create(
            school=self.school, name=name, term=term, year=year,
            school_section=section, sub_section=None, status="closed",
            classes=list(classes),
        )

    def _publish_at(self, exam, when, class_name="Grade 7"):
        sub = MarkSubmission.objects.create(
            school=self.school, teacher=self.teacher, subject=self.subject,
            school_section=exam.school_section, sub_section=exam.sub_section,
            class_name=class_name, stream="Main",
            exam_name=exam.name, term=exam.term, year=exam.year,
            status="published",
        )
        MarkSubmission.all_objects.filter(pk=sub.pk).update(published_at=when)
        return sub

    def test_default_exam_is_the_most_recently_published(self):
        """Academic order would pick Term 2; the LAST publish must win."""
        from datetime import timedelta

        from django.utils import timezone

        e_term1 = self._exam("Opener Exam", "Term 1")
        e_term2 = self._exam("End of Term", "Term 2")
        now = timezone.now()
        self._publish_at(e_term2, now - timedelta(days=5))
        self._publish_at(e_term1, now)          # Term 1 published LAST
        resp = self.client.get(reverse("analyse_exam"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["exam"].pk, e_term1.pk)

    def test_default_exam_falls_back_to_academic_order_when_nothing_published(self):
        self._exam("Opener Exam", "Term 2")
        e_new = self._exam("End of Term", "Term 3")
        resp = self.client.get(reverse("analyse_exam"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["exam"].pk, e_new.pk)

    def test_default_exam_scopes_to_the_named_grade(self):
        """Grade-scoped default follows THAT grade's own publish clock,
        not the school-wide latest (here a Grade 8 publish)."""
        from datetime import timedelta

        from django.utils import timezone

        e_g7 = self._exam("Grade 7 Opener", "Term 1", classes=["Grade 7"])
        e_g8 = self._exam("Grade 8 Opener", "Term 2", classes=["Grade 8"])
        now = timezone.now()
        self._publish_at(e_g8, now, class_name="Grade 8")
        self._publish_at(e_g7, now - timedelta(days=3), class_name="Grade 7")
        resp = self.client.get(reverse("analyse_exam") + "?class_name=Grade%207")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["exam"].pk, e_g7.pk)

    def test_mismatched_exam_and_grade_swaps_to_a_covering_exam(self):
        e_g8 = self._exam("Only Grade 8", "Term 3", classes=["Grade 8"])
        e_g7 = self._exam("Covers Grade 7", "Term 3", classes=["Grade 7"])
        resp = self.client.get(
            "%s?exam_id=%d&class_name=Grade%%207"
            % (reverse("analyse_exam"), e_g8.pk)
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["exam"].pk, e_g7.pk)

    def test_auto_detected_grade_is_deterministic_and_section_scoped(self):
        """Bare URL: pick the alphabetically-first grade of the exam's OWN
        section — a same-named PRIMARY summary (Grade 1) must never win."""
        jss_exam = self._exam("Shared Name", "Term 3", section="JSS")
        Exam.objects.create(
            school=self.school, name="Shared Name", term="Term 3", year=2026,
            school_section="PRIMARY", sub_section="LOWER", status="closed",
            classes=[],
        )
        for g in ("Grade 9", "Grade 7"):
            Student.objects.create(
                school=self.school, name="Detect %s" % g,
                admission_no="DET-%s" % g.replace(" ", ""),
                guardian=self.guardian, school_section="JSS",
                class_name=g, stream="Main",
            )
        for s in Student.all_objects.filter(school=self.school,
                                             school_section="JSS"):
            ExamSummary.objects.create(
                school=self.school, student=s, exam_name="Shared Name",
                term="Term 3", year=2026, school_section="JSS",
            )
        g1 = Student.objects.create(
            school=self.school, name="Detect Grade 1", admission_no="DET-G1",
            guardian=self.guardian, school_section="PRIMARY",
            sub_section="LOWER", class_name="Grade 1", stream="Main",
        )
        ExamSummary.objects.create(
            school=self.school, student=g1, exam_name="Shared Name",
            term="Term 3", year=2026, school_section="PRIMARY",
            sub_section="LOWER",
        )
        resp = self.client.get(
            "%s?exam_id=%d" % (reverse("analyse_exam"), jss_exam.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["grade_name"], "Grade 7")

    def test_grade_and_exam_selectors_are_wired(self):
        """Grade dropdown redirects with ?class_name= (was a no-op TODO)
        and the exam dropdown click carries the selected grade."""
        self._exam("Opener Exam", "Term 3")
        resp = self.client.get(reverse("analyse_exam"))
        html = resp.content.decode()
        # Exam click handler reads the SELECTED grade (gradeSel.value) …
        self.assertIn("gradeSel.value", html)
        # … and both handlers navigate with ?class_name=; the grade
        # selector change handler itself was an empty TODO before.
        self.assertIn("class_name=", html)
        self.assertIn("gradeSelect.addEventListener", html)


class TestAnalyseSnapshotScopingAndNumbers(ExamSpaceBase):
    """Snapshot-backed analysis: grade-scoped rows (no phantom cross-grade
    streams) and accurate per-subject KPIs / fine-grained distribution."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.exam = Exam.objects.create(
            school=cls.school, name="KNEC ASSESSMENT", term="Term 3",
            year=2026, school_section="JSS", sub_section=None,
            status="closed", classes=["Grade 7", "Grade 8"],
        )
        cls.g7 = {}
        for i, (stream, gender) in enumerate(
                [("Blue", "Male"), ("Blue", "Female"),
                 ("Yellow", "Male"), ("Yellow", "Female")], start=1):
            cls.g7[i] = Student.objects.create(
                school=cls.school, name="Sat Learner %d" % i,
                admission_no="SAT-%d" % i, guardian=cls.guardian,
                school_section="JSS", class_name="Grade 7",
                stream=stream, gender=gender,
            )
        cls.g8 = Student.objects.create(
            school=cls.school, name="Other Grade Learner", admission_no="SAT-G8",
            guardian=cls.guardian, school_section="JSS",
            class_name="Grade 8", stream="Main", gender="Male",
        )

        def card(sid, name, stream, total_marks, total_points, plv):
            return {str(sid): {
                "student_id": sid, "name": name, "gender": "Male",
                "stream": stream, "stream_rank": 1, "grade_rank": 1,
                "admission_no": "SAT-%d" % sid,
                "total_marks": total_marks, "total_points": total_points,
                "subject_count": 4, "overall_plv": plv, "marks": {},
                "mean_points": total_points / 4,
            }}

        cls.blue_cards = {
            **card(cls.g7[1].pk, "Sat Learner 1", "Blue", 80, 8, "BE1"),
            **card(cls.g7[2].pk, "Sat Learner 2", "Blue", 40, 4, "ME2"),
        }
        cls.yellow_cards = {
            **card(cls.g7[3].pk, "Sat Learner 3", "Yellow", 160, 16, "BE1"),
            **card(cls.g7[4].pk, "Sat Learner 4", "Yellow", 64, 6, "AE1"),
        }
        blue_sheet = {
            "121": {"student_count": 2, "total_score": 60, "mean_score": 30.0,
                    "mean_points": 3.0, "highest": 40, "lowest": 20,
                    "absent_count": 0,
                    "distribution": {"BE1": 1, "ME2": 1}},
        }
        yellow_sheet = {
            "121": {"student_count": 2, "total_score": 20, "mean_score": 10.0,
                    "mean_points": 1.0, "highest": 15, "lowest": 5,
                    "absent_count": 0,
                    "distribution": {"BE1": 1, "AE1": 1}},
        }
        for stream, cards, sheet in (
                ("Blue", cls.blue_cards, blue_sheet),
                ("Yellow", cls.yellow_cards, yellow_sheet)):
            ExamResultSnapshot.all_objects.create(
                school=cls.school, term="Term 3", year=2026,
                exam_name="KNEC ASSESSMENT", class_name="Grade 7",
                stream=stream, school_section="JSS", sub_section=None,
                report_card_data=cards, broadsheet_data=sheet,
            )
        # Grade 8's own snapshot under the SAME exam name — pooling it used
        # to yield 5 sitters and a phantom "Grade 7 Main" row.
        ExamResultSnapshot.all_objects.create(
            school=cls.school, term="Term 3", year=2026,
            exam_name="KNEC ASSESSMENT", class_name="Grade 8",
            stream="Main", school_section="JSS", sub_section=None,
            report_card_data={
                str(cls.g8.pk): {
                    "student_id": cls.g8.pk, "name": "Other Grade Learner",
                    "gender": "Male", "stream": "Main", "stream_rank": 1,
                    "grade_rank": 1, "admission_no": "SAT-G8",
                    "total_marks": 100, "total_points": 10,
                    "subject_count": 4, "overall_plv": "BE1", "marks": {},
                    "mean_points": 2.5,
                },
            },
            broadsheet_data={},
        )

    def _page(self):
        resp = self.client.get(
            "%s?exam_id=%d&class_name=Grade%%207"
            % (reverse("analyse_exam"), self.exam.pk)
        )
        self.assertEqual(resp.status_code, 200)
        return resp

    def test_snapshot_is_scoped_to_the_grade(self):
        resp = self._page()
        forms = [r["form"] for r in resp.context["grade_breakdown"]]
        self.assertEqual(forms, ["Grade 7 Blue", "Grade 7 Yellow"])
        self.assertNotIn("Grade 7 Main", forms)
        self.assertEqual(resp.context["grade_name"], "Grade 7")
        self.assertEqual(resp.context["students_who_sat"], 4)
        self.assertEqual(resp.context["total_students"], 4)
        self.assertEqual(resp.context["streams"], ["Blue", "Yellow"])

    def test_kpis_are_per_subject_and_within_scale(self):
        resp = self._page()
        # Totals sum across 4 students x 4 subjects = 16 subject entries;
        # per-subject means: 344/16 = 21.5 % and 34/16 = 2.125 points.
        # The old code divided by students, inflating both past scale.
        self.assertEqual(resp.context["overall_mean_marks"], 21.5)
        self.assertEqual(resp.context["overall_mean_points"], 2.125)
        self.assertLessEqual(resp.context["overall_mean_marks"], 100)
        self.assertLess(resp.context["overall_mean_points"], 4 * 4)
        self.assertEqual(resp.context["mm_change"], 0)  # no previous exam

    def test_breakdown_rows_carry_levels_devs_and_performance(self):
        resp = self._page()
        levels = resp.context["ordered_levels"]
        self.assertEqual(levels, ["EE1", "EE2", "ME1", "ME2",
                                  "AE1", "AE2", "BE1", "BE2"])
        rows = {r["form"]: r for r in resp.context["grade_breakdown"]}
        blue, yellow = rows["Grade 7 Blue"], rows["Grade 7 Yellow"]
        self.assertEqual(sum(blue[l] for l in levels), 2)
        self.assertEqual(sum(yellow[l] for l in levels), 2)
        self.assertEqual(blue["mean_marks"], 15.0)
        self.assertEqual(yellow["mean_marks"], 28.0)
        self.assertEqual(blue["mean_points"], 1.5)
        self.assertEqual(yellow["mean_points"], 2.75)
        self.assertEqual(blue["mm_dev"], -6.5)
        self.assertEqual(yellow["mm_dev"], 6.5)
        self.assertNotEqual(blue["mp_dev"], 0)
        for r in (blue, yellow):
            self.assertIn(r["performance_level"], levels)
        total = resp.context["total_row"]
        self.assertEqual(total["entries"], 4)
        self.assertEqual(total["mean_marks"], 21.5)
        self.assertEqual(total["performance_level"], "BE1")
        self.assertEqual(sum(total[l] for l in levels), 4)

    def test_subject_breakdowns_are_populated(self):
        resp = self._page()
        self.assertIn("Mathematics", resp.context["subject_names"])
        import json

        sb = json.loads(resp.context["subject_breakdowns_json"])
        self.assertIn("Mathematics", sb)
        bd = sb["Mathematics"]
        self.assertEqual(len(bd["rows"]), 2)
        self.assertEqual({r["form"] for r in bd["rows"]},
                         {"Grade 7 Blue", "Grade 7 Yellow"})
        for r in bd["rows"]:
            self.assertIn("mm_dev", r)
            self.assertIn("mp_dev", r)
            self.assertIn(r["performance_level"],
                          resp.context["ordered_levels"])
        total = bd["total"]
        self.assertEqual(total["entries"], 4)
        self.assertEqual(total["BE1"], 2)
        self.assertEqual(total["mean_marks"], 20.0)  # (60+20)/4
        self.assertEqual(total["mean_points"], 2.0)  # (3*2+1*2)/4


# ══════════════════════════════════════════════════════════════════════
# G. Capture sheet gate: open vs submitted rendering
# ══════════════════════════════════════════════════════════════════════
class TestCaptureGateRender(ExamSpaceBase):
    """The mark-capture sheet must render a confirmable gate while the
    assessment is open, and a clean read-only view once it is submitted.

    The JS reads data-submitted/data-locked off #enInlineSubmit *after*
    initMarkEntry has already relocated it, so that element must stay in
    the DOM (hidden, never removed) — removing it made initTotalGate treat
    a published sheet as open, gate the grid with pointer-events:none, and
    leave no confirm button to release it (frozen sheet on every
    submitted/locked assessment)."""

    SCORE_RE = re.compile(r'class="en-score js-score".*?onfocus', re.S)

    def _sheet(self, exam, assignment):
        return self.client.get(reverse("upload_results"), {
            "exam_id": exam.pk, "assignment_id": assignment.pk,
        })

    def _make_assignment(self):
        from .models import SubjectAssignment

        return SubjectAssignment.objects.create(
            school=self.school, teacher_profile=self.teacher,
            subject=self.subject, class_name="Grade 2", stream="Main",
            school_section="PRIMARY", sub_section="LOWER",
        )

    def _score_input(self, html):
        m = self.SCORE_RE.search(html)
        self.assertIsNotNone(m, "score input not rendered")
        return m.group(0)

    def test_open_sheet_renders_confirmable_gate(self):
        self.create("Gate Open", classes=["Grade 2"])
        exam = self.exams.get(name="Gate Open")
        assignment = self._make_assignment()
        Student.objects.create(
            school=self.school, name="Gate Learner", admission_no="GATE-1",
            guardian=self.guardian, school_section="PRIMARY",
            sub_section="LOWER", class_name="Grade 2", stream="Main",
        )

        resp = self._sheet(exam, assignment)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertFalse(resp.context["is_submitted"])
        self.assertIn('id="totalGateBtn"', html)
        self.assertIn('data-submitted="0"', html)
        self.assertNotIn("readonly", self._score_input(html),
                         "open sheet must accept scores")
        self.assertNotIn("submitBar.remove()", html)
        self.assertIn("submitBar.style.display = 'none'", html)

    def test_submitted_sheet_is_clean_read_only_without_fake_gate(self):
        self.create("Gate Done", classes=["Grade 2"])
        exam = self.exams.get(name="Gate Done")
        assignment = self._make_assignment()
        self.make_submission("Grade 2", "Gate Done", status="submitted")

        resp = self._sheet(exam, assignment)
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertTrue(resp.context["is_submitted"])
        self.assertNotIn('id="totalGateBtn"', html,
                         "submitted sheet must not offer a confirm button")
        self.assertIn('data-submitted="1"', html,
                      "initTotalGate re-reads this attribute")
        self.assertIn('id="enInlineSubmit"', html,
                      "submit bar must stay in the DOM for initTotalGate")
        self.assertIn("readonly", self._score_input(html),
                      "submitted sheet must be read-only")
        self.assertNotIn("submitBar.remove()", html)
        self.assertIn("submitBar.style.display = 'none'", html)
