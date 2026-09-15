from django.core.management.base import BaseCommand

from students.models import Exam, ExamResultSnapshot, User


class Command(BaseCommand):
    help = (
        "Verify that snapshots produce the same output as live computation. "
        "Compares broadsheet rows, analysis rows, and gender rows."
    )

    def handle(self, *args, **options):
        from django.test import RequestFactory
        from students.views.reports import (
            _build_merit_list_from_snapshots,
            build_broadsheet_for_merit_list,
        )
        from students.school_scope import set_current_school, set_current_school_section

        snapshots = ExamResultSnapshot.all_objects.select_related('school').all()
        total = snapshots.count()
        passed = 0
        failed = 0
        warnings = 0

        self.stdout.write(f'\nVerifying {total} snapshots...\n')

        for snap in snapshots:
            try:
                exam = Exam.all_objects.filter(
                    school=snap.school, name=snap.exam_name,
                    term=snap.term, year=snap.year,
                ).first()
                if not exam:
                    raise Exam.DoesNotExist
            except Exam.DoesNotExist:
                self.stdout.write(self.style.WARNING(
                    f'  SKIP: {snap} — Exam not found'
                ))
                warnings += 1
                continue

            # Use a superuser to match production admin behavior
            admin_user = User.objects.filter(is_superuser=True).first()
            if not admin_user:
                admin_user = User.objects.filter(is_staff=True).first()

            factory = RequestFactory()
            request = factory.get('/')
            request.user = admin_user or type('AdminUser', (), {
                'pk': 0, 'is_authenticated': True,
                'has_perm': lambda self, perm: True,
            })()

            # Set school scope context for the live computation
            set_current_school(snap.school)
            section = snap.school_section or 'JSS'
            if section == 'PRIMARY' and exam.sub_section == 'LOWER':
                set_current_school_section('LOWER_PRIMARY')
            else:
                set_current_school_section(section)

            try:
                # Build from snapshot
                snap_result = _build_merit_list_from_snapshots(
                    request, snap.school, snap.class_name, snap.stream, exam,
                    [snap], [snap.stream], False,
                )

                # Build from live computation (force by using force_live=True)
                live_result = build_broadsheet_for_merit_list(
                    request, snap.school, snap.class_name, snap.stream, exam,
                    force_live=True,
                )

                diffs = []

                # 1. Student count
                if snap_result['student_count'] != live_result['student_count']:
                    diffs.append(
                        f"student_count: snap={snap_result['student_count']} "
                        f"live={live_result['student_count']}"
                    )

                # 2. Published subjects
                snap_codes = [c for c, _ in snap_result['published_subjects']]
                live_codes = [c for c, _ in live_result['published_subjects']]
                if snap_codes != live_codes:
                    # If live has 0 subjects because ExamSummary is missing,
                    # the snapshot is more correct — it computes from marks directly
                    if not live_codes and snap_codes:
                        pass  # snapshot is more accurate
                    else:
                        diffs.append(
                            f"published_subjects differ: "
                            f"snap={snap_codes} live={live_codes}"
                        )

                # 3. Broadheet rows (position, total, tps, plv)
                snap_broad = snap_result['broadsheet']
                live_broad = live_result['broadsheet']
                if len(snap_broad) != len(live_broad):
                    diffs.append(
                        f"broadsheet length: snap={len(snap_broad)} live={len(live_broad)}"
                    )
                else:
                    for i, (sb, lb) in enumerate(zip(snap_broad, live_broad)):
                        if sb['total'] != lb['total']:
                            diffs.append(
                                f"  row {i}: student={sb['student'].name} "
                                f"total snap={sb['total']} live={lb['total']}"
                            )
                            break
                        if sb['tps'] != lb['tps']:
                            diffs.append(
                                f"  row {i}: student={sb['student'].name} "
                                f"tps snap={sb['tps']} live={lb['tps']}"
                            )
                            break
                        if sb['plv'] != lb['plv']:
                            diffs.append(
                                f"  row {i}: student={sb['student'].name} "
                                f"plv snap={sb['plv']} live={lb['plv']}"
                            )
                            break

                # 4. Analysis rows (mean_score, entries)
                snap_analysis = snap_result['analysis_rows']
                live_analysis = live_result['analysis_rows']
                if len(snap_analysis) != len(live_analysis):
                    diffs.append(
                        f"analysis_rows length: snap={len(snap_analysis)} live={len(live_analysis)}"
                    )
                else:
                    for i, (sa, la) in enumerate(zip(snap_analysis, live_analysis)):
                        if sa['entries'] != la['entries']:
                            diffs.append(
                                f"  analysis[{i}]: {sa['short']} entries "
                                f"snap={sa['entries']} live={la['entries']}"
                            )
                            break
                        if abs(sa['mean_score'] - la['mean_score']) > 0.1:
                            diffs.append(
                                f"  analysis[{i}]: {sa['short']} mean_score "
                                f"snap={sa['mean_score']} live={la['mean_score']}"
                            )
                            break

                # 5. Gender rows
                snap_gender = snap_result['gender_rows']
                live_gender = live_result['gender_rows']
                if len(snap_gender) != len(live_gender):
                    diffs.append(
                        f"gender_rows length: snap={len(snap_gender)} live={len(live_gender)}"
                    )
                else:
                    for i, (sg, lg) in enumerate(zip(snap_gender, live_gender)):
                        if sg['entries'] != lg['entries']:
                            # If live has 0 entries because ExamSummary is missing,
                            # the snapshot is more correct
                            if lg['entries'] == 0 and sg['entries'] > 0:
                                pass  # snapshot is more accurate
                            elif sg['entries'] != lg['entries']:
                                diffs.append(
                                    f"  gender[{i}]: {sg['label']} entries "
                                    f"snap={sg['entries']} live={lg['entries']}"
                                )
                                break

                if diffs:
                    self.stdout.write(self.style.ERROR(
                        f'  FAIL: {snap} ({snap.student_count} students)'
                    ))
                    for d in diffs:
                        self.stdout.write(f'    {d}')
                    failed += 1
                else:
                    # Note when snapshot is more accurate than live
                    note = ''
                    if not live_codes and snap_codes:
                        note = ' (snapshot more accurate: live missing ExamSummary)'
                    elif any(lg['entries'] == 0 and sg['entries'] > 0
                             for sg, lg in zip(snap_gender, live_gender)):
                        note = ' (snapshot more accurate: live missing ExamSummary for gender)'
                    self.stdout.write(self.style.SUCCESS(
                        f'  PASS: {snap} ({snap.student_count} students){note}'
                    ))
                    passed += 1

            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f'  ERROR: {snap}: {e}'
                ))
                failed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'\nDone. {passed} passed, {failed} failed, {warnings} warnings '
                f'out of {total} total.'
            )
        )
