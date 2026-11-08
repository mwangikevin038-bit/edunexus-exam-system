/* ═══════════════════════════════════════════════════════════════════════
   EDUNEXUS — DASHBOARD CALENDAR & EVENT MODAL
   Single source of truth for the dashboard's Upcoming Events card.

   Replaces the four verbatim copies that used to live inline in
   dashboard.html and dashboard_admin.html (desktop block + mobile block).

   Contract — the host template provides ONE card:

     <section id="dashCalendar"
              data-add-url="{% url 'add_event' %}"            (admin only)
              data-delete-pattern="{% url 'delete_event' event_id=0 %}">
       …header, nav, pills, viewport…
     </section>

     {% json_script "dash-term-events" term_events %}
     {% json_script "dash-school-events" school_events %}

   Role note: data-add-url / data-delete-pattern are rendered ONLY on the
   school-admin dashboard.  Teachers never receive event-write endpoints,
   so every mutation below quietly no-ops for them.
   ═══════════════════════════════════════════════════════════════════════ */
(function () {
    'use strict';

    var root = document.getElementById('dashCalendar');
    if (!root) return;

    /* ── Data ────────────────────────────────────────────────────────────
       Read from <script type="application/json"> tags so event names are
       never interpolated into executable JS. */
    function readJson(id) {
        var el = document.getElementById(id);
        if (!el) return [];
        try {
            var parsed = JSON.parse(el.textContent);
            return Array.isArray(parsed) ? parsed : [];
        } catch (e) {
            return [];
        }
    }

    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var FULL_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
                       'July', 'August', 'September', 'October', 'November', 'December'];
    /* Monday-first, matching the calendar grid header. */
    var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

    var termEvents = readJson('dash-term-events');
    var schoolEvents = readJson('dash-school-events');

    var addUrl = root.getAttribute('data-add-url') || '';
    var deletePattern = root.getAttribute('data-delete-pattern') || '';

    var view = 'month';
    var offset = 0;

    /* ── Element lookups (all scoped to this one card) ─────────────────── */
    var viewport = root.querySelector('[data-cal-viewport]');
    var mainEls = root.querySelectorAll('[data-cal-main]');
    var subEls = root.querySelectorAll('[data-cal-sub]');

    /* ── Date helpers ─────────────────────────────────────────────────── */
    function startOfWeek(d) {
        var s = new Date(d);
        var day = s.getDay();
        s.setDate(s.getDate() - (day === 0 ? 6 : day - 1));
        s.setHours(0, 0, 0, 0);
        return s;
    }

    function endOfWeek(d) {
        var e = startOfWeek(d);
        e.setDate(e.getDate() + 6);
        e.setHours(23, 59, 59, 999);
        return e;
    }

    function startOfMonth(d) { return new Date(d.getFullYear(), d.getMonth(), 1); }
    function endOfMonth(d) { return new Date(d.getFullYear(), d.getMonth() + 1, 0); }

    function today0() {
        var n = new Date();
        n.setHours(0, 0, 0, 0);
        return n;
    }

    function keyOf(d) {
        return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
    }

    /* The term windows a given calendar day falls inside. */
    function termDates() {
        var map = {};
        termEvents.forEach(function (te) {
            var sd = new Date(te.start);
            var ed = new Date(te.end);
            var cur = new Date(sd);
            while (cur <= ed) {
                map[keyOf(cur)] = { term: te.term, year: te.year, start: sd, end: ed };
                cur.setDate(cur.getDate() + 1);
            }
        });
        return map;
    }

    function schoolEventsOn(d) {
        var day = new Date(d.getFullYear(), d.getMonth(), d.getDate());
        return schoolEvents.filter(function (ev) {
            var sd = new Date(ev.start);
            var ed = new Date(ev.end);
            return day >= new Date(sd.getFullYear(), sd.getMonth(), sd.getDate()) &&
                   day <= new Date(ed.getFullYear(), ed.getMonth(), ed.getDate());
        });
    }

    function statusOf(sd, ed, now) {
        if (now >= sd && now <= ed) return 'active';
        return now < sd ? 'upcoming' : 'past';
    }

    /* ── Viewport: month grid ─────────────────────────────────────────── */
    function renderMonth() {
        var now = today0();
        var ref = new Date(now);
        ref.setMonth(ref.getMonth() + offset);

        var first = startOfMonth(ref);
        var last = endOfMonth(ref);
        var startIdx = first.getDay() === 0 ? 6 : first.getDay() - 1;
        var rows = Math.ceil((startIdx + last.getDate()) / 7);

        var terms = termDates();
        var html = '';

        DAYS.forEach(function (d) {
            html += '<div class="cal-header-cell">' + d + '</div>';
        });

        for (var i = 0; i < rows * 7; i++) {
            var dayNum = i - startIdx + 1;
            var cellDate = new Date(ref.getFullYear(), ref.getMonth(), dayNum);
            var inMonth = cellDate.getMonth() === ref.getMonth();
            var isToday = cellDate.getTime() === now.getTime();
            var cls = ['cal-day-cell'];
            if (!inMonth) cls.push('outside');
            if (isToday) cls.push('today');

            html += '<div class="' + cls.join(' ') + '">';
            if (inMonth) {
                html += '<span class="cal-day-num">' + cellDate.getDate() + '</span>';
                if (terms[keyOf(cellDate)] || schoolEventsOn(cellDate).length) {
                    html += '<span class="cal-dots">' +
                            (terms[keyOf(cellDate)] ? '<i class="cal-dot cal-dot-term"></i>' : '') +
                            (schoolEventsOn(cellDate).length ? '<i class="cal-dot cal-dot-school"></i>' : '') +
                            '</span>';
                }
            }
            html += '</div>';
        }
        viewport.innerHTML = '<div class="cal-grid">' + html + '</div>';
    }

    /* ── Viewport: week list ──────────────────────────────────────────── */
    function renderWeek() {
        var now = today0();
        var ref = new Date(now);
        ref.setDate(now.getDate() + offset * 7);
        var ws = startOfWeek(ref);
        var we = endOfWeek(ref);

        var html = '<div class="cal-week-list">';
        var d = new Date(ws);
        while (d <= we) {
            var isToday = d.toDateString() === now.toDateString();
            html += '<div class="cal-week-row' + (isToday ? ' today' : '') + '">' +
                    '<span class="cal-week-day">' + DAYS[d.getDay() === 0 ? 6 : d.getDay() - 1] + '</span>' +
                    '<span class="cal-week-date">' + d.getDate() + ' ' + MONTHS[d.getMonth()] + '</span>' +
                    '</div>';
            d.setDate(d.getDate() + 1);
        }
        html += '</div>';
        viewport.innerHTML = html;
    }

    /* ── Viewport: day detail ─────────────────────────────────────────── */
    function renderDay() {
        var now = today0();
        var ref = new Date(now);
        ref.setDate(now.getDate() + offset);

        var terms = termDates();
        var term = terms[keyOf(ref)];
        var custom = schoolEventsOn(ref);

        var html = '<div class="cal-day-detail">' +
                   '<div class="cal-day-detail-date">' +
                   DAYS[ref.getDay() === 0 ? 6 : ref.getDay() - 1] + ', ' +
                   ref.getDate() + ' ' + MONTHS[ref.getMonth()] + ' ' + ref.getFullYear() +
                   '</div>';

        if (term) {
            html += '<span class="cal-day-event-badge">' + term.term + ' ' + term.year + '</span>';
        }
        custom.forEach(function (ev) {
            html += '<span class="cal-day-event-badge custom-event">' +
                    escapeHtml(ev.name) + (ev.time ? ' at ' + escapeHtml(ev.time) : '') +
                    '</span>';
        });
        if (!term && !custom.length) {
            html += '<p class="empty-events-text">No events on this day</p>';
        }
        html += '</div>';
        viewport.innerHTML = html;
    }

    /* ── Viewport: list ───────────────────────────────────────────────── */
    function renderList() {
        var now = today0();
        if (!termEvents.length && !schoolEvents.length) {
            viewport.innerHTML = '<p class="empty-events-text">No events yet</p>';
            return;
        }

        var html = '<div class="cal-list-view">';

        termEvents.forEach(function (te) {
            var sd = new Date(te.start);
            var ed = new Date(te.end);
            var status = statusOf(sd, ed, now);
            var tone = status === 'active' ? 'active' : (status === 'upcoming' ? 'upcoming' : 'past');
            html += '<div class="cal-list-item is-' + tone + '">' +
                    '<div class="cal-list-label">' + escapeHtml(te.term + ' ' + te.year) + '</div>' +
                    '<div class="cal-list-dates">' + sd.getDate() + ' ' + MONTHS[sd.getMonth()] +
                    ' – ' + ed.getDate() + ' ' + MONTHS[ed.getMonth()] + ' ' + ed.getFullYear() + '</div>' +
                    '<span class="cal-list-status">' + capitalize(status) + '</span>' +
                    '</div>';
        });

        schoolEvents.forEach(function (ev) {
            var sd = new Date(ev.start);
            var ed = new Date(ev.end);
            var status = statusOf(sd, ed, now);
            var tone = status === 'active' ? 'active' : (status === 'upcoming' ? 'upcoming' : 'past');
            var dateStr = ev.date_mode === 'range'
                ? sd.getDate() + ' ' + MONTHS[sd.getMonth()] + ' – ' + ed.getDate() + ' ' +
                  MONTHS[ed.getMonth()] + ' ' + ed.getFullYear()
                : sd.getDate() + ' ' + MONTHS[sd.getMonth()] + ' ' + sd.getFullYear();
            var timeStr = ev.time ? ' at ' + ev.time : '';

            html += '<div class="cal-list-item is-' + tone + '">' +
                    '<div class="cal-list-label">' + escapeHtml(ev.name) + '</div>' +
                    '<div class="cal-list-dates">' + escapeHtml(dateStr + timeStr + ' — ' + ev.participants) + '</div>' +
                    '<span class="cal-list-status">' + capitalize(status) + '</span>';
            /* Delete is only wired up when the host page offered an endpoint. */
            if (deletePattern) {
                html += '<button class="cal-list-delete" type="button" data-delete-id="' + ev.id +
                        '" title="Delete event" aria-label="Delete ' + escapeHtml(ev.name) + '">' +
                        '<i class="ph ph-trash"></i></button>';
            }
            html += '</div>';
        });

        html += '</div>';
        viewport.innerHTML = html;
    }

    /* ── Viewport: chrome + dispatch ──────────────────────────────────── */
    function renderDateText() {
        var now = new Date();
        var ref = new Date(now);
        var mainText = '';
        var subText = '';

        if (view === 'month') {
            ref.setMonth(now.getMonth() + offset);
            mainText = FULL_MONTHS[ref.getMonth()];
            subText = String(ref.getFullYear());
        } else if (view === 'week') {
            ref.setDate(now.getDate() + offset * 7);
            var ws = startOfWeek(ref);
            var we = endOfWeek(ref);
            mainText = ws.getDate() + ' ' + MONTHS[ws.getMonth()] + ' – ' +
                       we.getDate() + ' ' + MONTHS[we.getMonth()];
            subText = String(we.getFullYear());
        } else if (view === 'day') {
            ref.setDate(now.getDate() + offset);
            mainText = ref.getDate() + ' ' + MONTHS[ref.getMonth()];
            subText = String(ref.getFullYear());
        } else {
            mainText = 'Upcoming Events';
            subText = '';
        }

        for (var i = 0; i < mainEls.length; i++) mainEls[i].textContent = mainText;
        for (var j = 0; j < subEls.length; j++) subEls[j].textContent = subText;
    }

    function renderViewport() {
        if (!viewport) return;
        if (view === 'month') renderMonth();
        else if (view === 'week') renderWeek();
        else if (view === 'day') renderDay();
        else renderList();
    }

    function render() {
        renderDateText();
        renderViewport();
    }

    /* ── Controls ─────────────────────────────────────────────────────── */
    function syncPills() {
        root.querySelectorAll('[data-cal-view]').forEach(function (btn) {
            btn.classList.toggle('is-active', btn.getAttribute('data-cal-view') === view);
        });
        root.querySelectorAll('[data-cal-tool]').forEach(function (btn) {
            btn.classList.toggle('is-active', btn.getAttribute('data-cal-tool') === view);
        });
    }

    root.addEventListener('click', function (e) {
        var prev = e.target.closest('[data-cal-prev]');
        var next = e.target.closest('[data-cal-next]');
        var pill = e.target.closest('[data-cal-view]');
        var tool = e.target.closest('[data-cal-tool]');
        var del = e.target.closest('[data-delete-id]');

        if (prev) { offset -= 1; render(); return; }
        if (next) { offset += 1; render(); return; }
        if (pill || tool) {
            view = (pill || tool).getAttribute('data-cal-view') ||
                   (pill || tool).getAttribute('data-cal-tool');
            offset = 0;
            syncPills();
            render();
            return;
        }
        if (del) {
            deleteEvent(parseInt(del.getAttribute('data-delete-id'), 10));
        }
    });

    /* ── Event CRUD (admin only — endpoints absent for teachers) ──────── */
    function post(url, body) {
        var token = (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || '';
        return fetch(url, {
            method: 'POST',
            headers: { 'X-CSRFToken': token },
            body: body || undefined
        }).then(function (r) { return r.json(); });
    }

    function deleteEvent(id) {
        if (!deletePattern) return;
        if (!window.confirm('Delete this event?')) return;
        post(deletePattern.replace('/0/', '/' + id + '/'))
            .then(function (data) {
                if (!data.ok) { window.alert(data.error || 'Failed to delete'); return; }
                schoolEvents = schoolEvents.filter(function (ev) { return ev.id !== id; });
                render();
            })
            .catch(function (err) { window.alert('Network error: ' + err); });
    }

    /* ═══ ADD EVENT MODAL ═══════════════════════════════════════════════
       No-ops entirely when the host page has no modal (teacher dashboard). */
    var modal = document.getElementById('eventModal');

    function openModal() {
        if (!modal) return;
        modal.classList.add('open');
        document.body.style.overflow = 'hidden';
        var first = modal.querySelector('#evtName');
        if (first) first.focus();
    }

    function closeModal() {
        if (!modal) return;
        modal.classList.remove('open');
        document.body.style.overflow = '';
        var picker = document.getElementById('datePickerPopup');
        if (picker) picker.classList.remove('open');
    }

    if (addUrl && modal) {
        document.addEventListener('click', function (e) {
            if (e.target.closest('[data-cal-add]')) openModal();
            if (e.target.closest('[data-modal-close]')) closeModal();
        });
        modal.addEventListener('click', function (e) {
            if (e.target === modal) closeModal();
        });
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && modal.classList.contains('open')) closeModal();
        });
    }

    /* ── Date mode (single / range) ───────────────────────────────────── */
    document.addEventListener('change', function (e) {
        if (!e.target.matches('input[name="dateMode"]')) return;
        var label = document.getElementById('pickDateLabel');
        if (label) label.textContent = e.target.value === 'range' ? 'Start date' : 'Pick date';
    });

    /* ── Participants dropdown ────────────────────────────────────────── */
    document.addEventListener('click', function (e) {
        var trigger = e.target.closest('#participantSelect');
        var item = e.target.closest('[data-participant]');
        var dd = document.getElementById('participantDropdown');
        if (!dd) return;

        if (trigger) { dd.classList.toggle('open'); return; }
        if (item) {
            var display = document.getElementById('participantDisplay');
            if (display) display.textContent = item.getAttribute('data-participant');
            dd.classList.remove('open');
            return;
        }
        if (!e.target.closest('#participantSelect')) dd.classList.remove('open');
    });

    /* ── Custom date/time picker ──────────────────────────────────────── */
    var edpDate = new Date();
    var edpHour = 14;
    var edpMin = 0;

    function renderEDP() {
        var title = document.getElementById('edpMonthYear');
        var grid = document.getElementById('edpGrid');
        if (!title || !grid) return;

        title.textContent = FULL_MONTHS[edpDate.getMonth()] + ' ' + edpDate.getFullYear();

        var first = new Date(edpDate.getFullYear(), edpDate.getMonth(), 1);
        var last = new Date(edpDate.getFullYear(), edpDate.getMonth() + 1, 0);
        var startDay = first.getDay();
        var today = today0();
        var selected = new Date(edpDate.getFullYear(), edpDate.getMonth(), edpDate.getDate());
        var html = '';

        for (var i = 0; i < startDay; i++) {
            var lead = new Date(edpDate.getFullYear(), edpDate.getMonth(), -(startDay - i - 1));
            html += '<div class="edp-day outside">' + lead.getDate() + '</div>';
        }
        for (var j = 1; j <= last.getDate(); j++) {
            var cell = new Date(edpDate.getFullYear(), edpDate.getMonth(), j);
            var cls = 'edp-day';
            if (cell.getTime() === today.getTime()) cls += ' today';
            if (cell.getTime() === selected.getTime()) cls += ' selected';
            html += '<div class="' + cls + '" data-edp-day="' + j + '">' + j + '</div>';
        }
        var used = startDay + last.getDate();
        for (var k = 1; k <= 42 - used; k++) {
            html += '<div class="edp-day outside">' + k + '</div>';
        }
        grid.innerHTML = html;
    }

    function pad(n) { return n < 10 ? '0' + n : String(n); }

    function syncTime() {
        var h = document.getElementById('edpHour');
        var m = document.getElementById('edpMin');
        if (h) h.textContent = pad(edpHour);
        if (m) m.textContent = pad(edpMin);
    }

    document.addEventListener('click', function (e) {
        var day = e.target.closest('[data-edp-day]');
        var monthNav = e.target.closest('[data-edp-month]');
        var timeAdj = e.target.closest('[data-edp-time]');
        var open = e.target.closest('[data-edp-open]');
        var close = e.target.closest('[data-edp-close]');
        var set = e.target.closest('[data-edp-set]');

        if (open) {
            var popup = document.getElementById('datePickerPopup');
            if (popup) { popup.classList.add('open'); renderEDP(); }
            return;
        }
        if (close) {
            var pc = document.getElementById('datePickerPopup');
            if (pc) pc.classList.remove('open');
            return;
        }
        if (monthNav) {
            edpDate.setMonth(edpDate.getMonth() + parseInt(monthNav.getAttribute('data-edp-month'), 10));
            renderEDP();
            return;
        }
        if (day) {
            edpDate = new Date(edpDate.getFullYear(), edpDate.getMonth(),
                                parseInt(day.getAttribute('data-edp-day'), 10));
            renderEDP();
            return;
        }
        if (timeAdj) {
            var unit = timeAdj.getAttribute('data-edp-time');
            var dir = parseInt(timeAdj.getAttribute('data-dir'), 10);
            if (unit === 'h') edpHour = (edpHour + dir + 24) % 24;
            else edpMin = (edpMin + dir * 15 + 60) % 60;
            syncTime();
            return;
        }
        if (set) {
            var display = document.getElementById('datePickerDisplay');
            var value = edpDate.getFullYear() + '-' + pad(edpDate.getMonth() + 1) + '-' +
                        pad(edpDate.getDate()) + ' ' + pad(edpHour) + ':' + pad(edpMin);
            if (display) {
                display.textContent = MONTHS[edpDate.getMonth()] + ' ' + edpDate.getDate() + ', ' +
                                      edpDate.getFullYear() + ' ' + pad(edpHour) + ':' + pad(edpMin);
            }
            root.setAttribute('data-selected-datetime', value);
            var popup = document.getElementById('datePickerPopup');
            if (popup) popup.classList.remove('open');
        }
    });

    /* ── Save ─────────────────────────────────────────────────────────── */
    document.addEventListener('click', function (e) {
        if (!e.target.closest('[data-event-save]')) return;

        var nameEl = document.getElementById('evtName');
        var name = nameEl ? nameEl.value.trim() : '';
        if (!name) { window.alert('Please enter an event name.'); return; }

        var modeEl = document.querySelector('input[name="dateMode"]:checked');
        var dateMode = modeEl ? modeEl.value : 'single';

        var stamp = root.getAttribute('data-selected-datetime') || '';
        var parts = stamp.split(' ');
        var eventDate = parts[0] || '';
        var eventTime = parts[1] || '';
        if (!eventDate) { window.alert('Please pick a date.'); return; }

        var endDate = '';
        if (dateMode === 'range') {
            endDate = window.prompt('Enter end date (YYYY-MM-DD):', eventDate);
            if (!endDate) return;
        }

        var display = document.getElementById('participantDisplay');
        var participants = display ? display.textContent.trim() : 'All';
        if (participants === 'Select participants') participants = 'All';

        var fd = new FormData();
        fd.append('name', name);
        fd.append('date_mode', dateMode);
        fd.append('event_date', eventDate);
        fd.append('end_date', endDate);
        fd.append('time', eventTime);
        fd.append('participants', participants);

        post(addUrl, fd)
            .then(function (data) {
                if (!data.ok) { window.alert(data.error || 'Failed to save event'); return; }
                schoolEvents.push(data.event);
                closeModal();
                render();
            })
            .catch(function (err) { window.alert('Network error: ' + err); });
    });

    /* ── Boot ─────────────────────────────────────────────────────────── */
    syncPills();
    syncTime();
    render();
})();
