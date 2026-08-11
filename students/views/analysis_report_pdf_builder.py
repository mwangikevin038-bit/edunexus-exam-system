"""Build HTML content for the Analysis Report PDF (WeasyPrint)."""


def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;') if s else ''


def _plv_label(plv, labels):
    return labels.get(plv, plv) if plv else '-'


def _change_html(val, decimals=4):
    v = float(val or 0)
    cls = 'up' if v > 0 else ('down' if v < 0 else 'neutral')
    sign = '+' if v > 0 else ''
    return f'{sign}{v:.{decimals}f}'


def build_table_row(row, levels, extra_cols=None, highlight=False):
    cls = ' class="best-row"' if highlight else ''
    h = f'<tr{cls}><td>{_esc(row.get("form", ""))}</td>'
    for lvl in levels:
        h += f'<td>{row.get(lvl, 0)}</td>'
    h += f'<td>{row.get("X", 0)}</td>'
    h += f'<td>{row.get("Y", 0)}</td>'
    h += f'<td>{row.get("entries", 0)}</td>'
    h += f'<td>{row.get("mean_marks", 0)}</td>'
    h += f'<td>{row.get("mm_dev", 0)}</td>'
    h += f'<td>{row.get("mean_points", 0)}</td>'
    h += f'<td>{row.get("mp_dev", 0)}</td>'
    h += f'<td>{_plv_label(row.get("performance_level", "-"), {})}</td>'
    if extra_cols and 'teacher' in extra_cols:
        h += f'<td style="text-align:left">{_esc(row.get("teacher", "-"))}</td>'
    h += '</tr>'
    return h


def build_total_row(row, levels, extra_cols=None):
    h = f'<tr class="total-row"><td>{_esc(row.get("form", "Total"))}</td>'
    for lvl in levels:
        h += f'<td>{row.get(lvl, 0)}</td>'
    h += f'<td>{row.get("X", 0)}</td>'
    h += f'<td>{row.get("Y", 0)}</td>'
    h += f'<td>{row.get("entries", 0)}</td>'
    h += f'<td>{row.get("mean_marks", 0)}</td>'
    h += f'<td>{row.get("mm_dev", 0)}</td>'
    h += f'<td>{row.get("mean_points", 0)}</td>'
    h += f'<td>{row.get("mp_dev", 0)}</td>'
    h += f'<td>{_plv_label(row.get("performance_level", "-"), {})}</td>'
    if extra_cols and 'teacher' in extra_cols:
        h += f'<td style="text-align:left">{_esc(row.get("teacher", "-"))}</td>'
    h += '</tr>'
    return h


def build_breakdown_table(rows, total_row, levels, show_teacher=False):
    h = '<table>'
    h += '<thead><tr><th style="text-align:left">Form</th>'
    for lvl in levels:
        h += f'<th>{lvl}</th>'
    h += '<th>X</th><th>Y</th><th>Entries</th>'
    h += '<th>Mean Marks</th><th>MM Dev</th>'
    h += '<th>Mean Points</th><th>MP Dev</th>'
    h += '<th>Perf. Level</th>'
    if show_teacher:
        h += '<th>Teacher</th>'
    h += '</tr></thead><tbody>'

    best_idx = -1
    best_mp = -999
    for i, r in enumerate(rows):
        if (r.get('mean_points') or 0) > best_mp:
            best_mp = r.get('mean_points') or 0
            best_idx = i

    extra = {'teacher'} if show_teacher else set()
    for i, r in enumerate(rows):
        h += build_table_row(r, levels, extra, highlight=(i == best_idx))
    if total_row:
        h += build_total_row(total_row, levels, extra)
    h += '</tbody></table>'
    return h


def build_gender_table(rows, levels):
    h = '<table><thead><tr><th style="text-align:left">Gender</th>'
    for lvl in levels:
        h += f'<th>{lvl}</th>'
    h += '<th>X</th><th>Y</th><th>Entries</th>'
    h += '<th>Mean Marks</th><th>MM Dev</th>'
    h += '<th>Mean Points</th><th>MP Dev</th>'
    h += '<th>Perf. Level</th></tr></thead><tbody>'
    for row in rows:
        h += f'<tr><td>{_esc(row.get("gender", ""))}</td>'
        for lvl in levels:
            h += f'<td>{row.get(lvl, 0)}</td>'
        h += f'<td>{row.get("X", 0)}</td>'
        h += f'<td>{row.get("Y", 0)}</td>'
        h += f'<td>{row.get("entries", 0)}</td>'
        h += f'<td>{row.get("mean_marks", 0)}</td>'
        h += f'<td>{row.get("mm_dev", 0)}</td>'
        h += f'<td>{row.get("mean_points", 0)}</td>'
        h += f'<td>{row.get("mp_dev", 0)}</td>'
        h += f'<td>{_plv_label(row.get("performance_level", "-"), {})}</td></tr>'
    h += '</tbody></table>'
    return h


def build_top_students_table(students, total, rank_key='overall_rank', strm_rank_key='strm_rank', strm_total_key='strm_total', show_gender=True):
    h = '<table><thead><tr>'
    h += '<th>Adm No</th><th>Name</th><th>Stream</th><th>Strm Rank</th><th>Overall Rank</th>'
    h += '<th>Mean Marks</th><th>MM Dev</th><th>Total Marks</th>'
    h += '<th>Mean Points</th><th>MP Dev</th><th>Perf. Level</th>'
    if show_gender:
        h += '<th>Gender</th>'
    h += '</tr></thead><tbody>'
    for st in students:
        h += '<tr>'
        h += f'<td>{_esc(st.get("admission_no", ""))}</td>'
        h += f'<td style="text-align:left">{_esc(st.get("name", ""))}</td>'
        h += f'<td>{_esc(st.get("stream", ""))}</td>'
        h += f'<td>{st.get(strm_rank_key, "")}/{st.get(strm_total_key, "")}</td>'
        h += f'<td>{st.get(rank_key, "")}/{total}</td>'
        h += f'<td>{st.get("mean_marks", 0)}</td>'
        h += f'<td>{st.get("mm_dev", 0)}</td>'
        h += f'<td>{st.get("total_marks", 0)}</td>'
        h += f'<td>{st.get("mean_points", 0)}</td>'
        h += f'<td>{st.get("mp_dev", 0)}</td>'
        h += f'<td>{_plv_label(st.get("performance_level", "-"), {})}</td>'
        if show_gender:
            h += f'<td>{_esc(st.get("gender", ""))}</td>'
        h += '</tr>'
    h += '</tbody></table>'
    return h


def build_pot_table(pot_labels, streams_all, pot_streams_data):
    if not pot_labels:
        return ''
    h = '<table><thead><tr><th style="text-align:left">Exam</th>'
    for s in streams_all:
        h += f'<th>{_esc(s)}</th>'
    h += '</tr></thead><tbody>'
    for i, label in enumerate(pot_labels):
        h += f'<tr><td style="text-align:left">{_esc(label)}</td>'
        for s in streams_all:
            arr = pot_streams_data.get(s, [])
            val = arr[i] if i < len(arr) else None
            h += f'<td>{val if val is not None else "-"}</td>'
        h += '</tr>'
    h += '</tbody></table>'
    return h


def build_subject_table(rows, total_row, levels, show_teacher=True):
    h = '<table><thead><tr><th style="text-align:left">Form</th>'
    for lvl in levels:
        h += f'<th>{lvl}</th>'
    h += '<th>X</th><th>Y</th><th>Entries</th>'
    h += '<th>Mean Marks</th><th>MM Dev</th>'
    h += '<th>Mean Points</th><th>MP Dev</th>'
    h += '<th>Perf. Level</th>'
    if show_teacher:
        h += '<th>Subject Teacher</th>'
    h += '</tr></thead><tbody>'
    for r in rows:
        h += f'<tr><td>{_esc(r.get("form", ""))}</td>'
        for lvl in levels:
            h += f'<td>{r.get(lvl, 0)}</td>'
        h += f'<td>{r.get("X", 0)}</td>'
        h += f'<td>{r.get("Y", 0)}</td>'
        h += f'<td>{r.get("entries", 0)}</td>'
        h += f'<td>{r.get("mean_marks", 0)}</td>'
        h += f'<td>{r.get("mm_dev", 0)}</td>'
        h += f'<td>{r.get("mean_points", 0)}</td>'
        h += f'<td>{r.get("mp_dev", 0)}</td>'
        h += f'<td>{_plv_label(r.get("performance_level", "-"), {})}</td>'
        if show_teacher:
            h += f'<td style="text-align:left">{_esc(r.get("teacher", "-"))}</td>'
        h += '</tr>'
    if total_row:
        h += f'<tr class="total-row"><td>{_esc(total_row.get("form", "Total"))}</td>'
        for lvl in levels:
            h += f'<td>{total_row.get(lvl, 0)}</td>'
        h += f'<td>{total_row.get("X", 0)}</td>'
        h += f'<td>{total_row.get("Y", 0)}</td>'
        h += f'<td>{total_row.get("entries", 0)}</td>'
        h += f'<td>{total_row.get("mean_marks", 0)}</td>'
        h += f'<td>{total_row.get("mm_dev", 0)}</td>'
        h += f'<td>{total_row.get("mean_points", 0)}</td>'
        h += f'<td>{total_row.get("mp_dev", 0)}</td>'
        h += f'<td>{_plv_label(total_row.get("performance_level", "-"), {})}</td>'
        if show_teacher:
            h += f'<td style="text-align:left">{_esc(total_row.get("teacher", "-"))}</td>'
        h += '</tr>'
    h += '</tbody></table>'
    return h


def build_pdf_html(data):
    """Build the full HTML body content for the PDF report."""
    levels = data.get('ordered_levels', [])
    school = data.get('school', {})
    exam = data.get('exam', {})
    grade_name = data.get('grade_name', '')
    stream_filter = data.get('stream_filter', '')
    streams_all = data.get('streams_all', [])
    subjects = data.get('subject_names', [])
    subject_breakdowns = data.get('subject_breakdowns', {})

    parts = []

    # Accent bar
    parts.append('<div class="hdr-accent"></div>')

    # School header
    parts.append('<div class="school-hdr">')
    if school.get('logo'):
        parts.append(f'<img src="{school["logo"]}" alt="Logo" class="school-logo">')
    parts.append('<div class="school-info">')
    parts.append(f'<div class="school-name">{_esc(school.get("name", ""))}</div>')
    if school.get('motto'):
        parts.append(f'<div class="school-motto">{_esc(school["motto"])}</div>')
    if school.get('address'):
        parts.append(f'<div class="school-addr">Address: {_esc(school["address"])}</div>')
    if school.get('phone') or school.get('email'):
        contact = ''
        if school.get('phone'):
            contact += f'Tel {_esc(school["phone"])}'
        if school.get('phone') and school.get('email'):
            contact += ' &nbsp;|&nbsp; '
        if school.get('email'):
            contact += f'Email {_esc(school["email"])}'
        parts.append(f'<div class="school-contact">{contact}</div>')
    parts.append('</div></div>')

    # Exam banner
    banner_stream = f' {_esc(stream_filter)}' if stream_filter else ''
    parts.append(f'<div class="exam-banner">{_esc(grade_name)}{banner_stream} &ndash; {_esc(exam.get("name", ""))} &ndash; ({_esc(exam.get("year", ""))} {_esc(exam.get("term", ""))}) &ndash; REPORT</div>')

    # Summary row
    mp = float(data.get('overall_mean_points', 0))
    mm = float(data.get('overall_mean_marks', 0))
    mp_ch = float(data.get('mp_change', 0))
    mm_ch = float(data.get('mm_change', 0))
    mp_cls = 'up' if mp_ch > 0 else ('down' if mp_ch < 0 else 'neutral')
    mm_cls = 'up' if mm_ch > 0 else ('down' if mm_ch < 0 else 'neutral')
    mp_sign = '+' if mp_ch > 0 else ''
    mm_sign = '+' if mm_ch > 0 else ''

    parts.append('<div class="summary-row"><div class="summary-left"><div class="summary-means">')
    parts.append(f'<div class="mean-box"><div class="lbl">Mean Points</div><div class="val">{mp:.4f}</div><div class="chg {mp_cls}">{mp_sign}{mp_ch:.4f}</div></div>')
    parts.append(f'<div class="mean-box"><div class="lbl">Mean Marks</div><div class="val">{mm:.1f}%</div><div class="chg {mm_cls}">{mm_sign}{mm_ch:.1f}</div></div>')
    parts.append('</div>')
    parts.append(f'<div class="mean-box"><div class="lbl">Performance Level</div><div class="val" style="font-size:14px">{_plv_label(data.get("overall_plv", ""), data.get("plv_labels", {}))}</div></div>')
    parts.append('</div>')
    parts.append(f'<div class="summary-right"><div class="lbl">Students who sat</div><div class="val">{data.get("students_who_sat", 0)}</div><div class="sub">Students</div></div>')
    parts.append('</div>')

    # Subject Statistics
    parts.append('<div class="section-title">Subject Statistics</div>')
    parts.append('<table><thead><tr><th style="text-align:left">Name</th><th>Points</th><th>Change</th><th>Performance Level</th></tr></thead><tbody>')
    for sr in data.get('subject_rows', []):
        chg = sr.get('change', 0)
        sign = '+' if chg > 0 else ''
        parts.append(f'<tr><td>{_esc(sr.get("name", ""))}</td><td>{sr.get("points", 0)}</td><td>{sign}{chg:.4f}</td><td>{_plv_label(sr.get("performance_level", ""), data.get("plv_labels", {}))}</td></tr>')
    parts.append('</tbody></table>')

    # Grade Summary – Overall
    parts.append('<div class="section-title">Grade Summary – Overall</div>')
    parts.append(build_breakdown_table(
        data.get('grade_breakdown', []),
        data.get('total_row'),
        levels,
        show_teacher=True
    ))

    # Gender Performance Analysis
    parts.append('<div class="section-title">Gender Performance Analysis</div>')
    parts.append(build_gender_table(data.get('gender_perf_rows', []), levels))

    # Top Students – Overall
    parts.append('<div class="section-title">Top Students – Overall</div>')
    parts.append(build_top_students_table(
        data.get('top_students', []),
        data.get('total_overall', 0),
        rank_key='overall_rank',
        strm_rank_key='strm_rank',
        strm_total_key='strm_total',
        show_gender=True
    ))

    # Top Boys
    if data.get('top_boys'):
        parts.append('<div class="section-title">Top Students – Boys</div>')
        parts.append(build_top_students_table(
            data.get('top_boys', []),
            data.get('total_boys_in_grade', 0),
            rank_key='overall_boys_rank',
            strm_rank_key='boys_strm_rank',
            strm_total_key='boys_strm_total',
            show_gender=False
        ))

    # Top Girls
    if data.get('top_girls'):
        parts.append('<div class="section-title">Top Students – Girls</div>')
        parts.append(build_top_students_table(
            data.get('top_girls', []),
            data.get('total_girls_count', 0),
            rank_key='overall_girls_rank',
            strm_rank_key='girls_strm_rank',
            strm_total_key='girls_strm_total',
            show_gender=False
        ))

    # Performance Over Time
    pot_labels = data.get('pot_labels', [])
    if pot_labels:
        parts.append('<div class="section-title">Performance Over Time</div>')
        parts.append(build_pot_table(pot_labels, streams_all, data.get('pot_streams_data', {})))

    # Per-subject breakdowns
    for subj_name in subjects:
        bd = subject_breakdowns.get(subj_name)
        if not bd:
            continue
        parts.append('<div class="page-break"></div>')
        parts.append(f'<div class="subject-banner">{_esc(subj_name)}</div>')
        parts.append(f'<div class="section-title">Grade Summary – {_esc(subj_name)}</div>')
        parts.append(build_subject_table(
            bd.get('rows', []),
            bd.get('total'),
            levels,
            show_teacher=True
        ))

        gender_rows = bd.get('gender_rows', [])
        if gender_rows:
            parts.append(f'<div class="section-title">Gender Performance – {_esc(subj_name)}</div>')
            parts.append(build_gender_table(gender_rows, levels))

    return '\n'.join(parts)
