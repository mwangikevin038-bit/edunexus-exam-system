/*
 * context_cascade.js — cascading Class → Assessment context picker.
 *
 * Used by Find Report (report_card_lookup) and Report Cards (report_card_select
 * desktop/mobile, primary variant). The server renders one flat pool of every
 * published context (grouped by class in <optgroup>) plus a class <select>.
 * This script:
 *   1. Filters the assessment select to the chosen class on load,
 *   2. On class change, carries the same exam/term/year over when that class
 *      has it (otherwise that class's newest context), then submits the form,
 *   3. Leaves the assessment select's inline onchange to submit on exam change.
 *
 * No-JS fallback: the server-rendered select still lists every context grouped
 * by class, so switching keeps working the old way.
 */
(function () {
    'use strict';

    function readPool(examSel) {
        var pool = [];
        var groups = examSel.querySelectorAll('optgroup');
        if (groups.length) {
            Array.prototype.forEach.call(groups, function (og) {
                var group = og.getAttribute('label') || '';
                Array.prototype.forEach.call(og.querySelectorAll('option'), function (o) {
                    pool.push({
                        value: o.value,
                        text: (o.textContent || '').trim(),
                        group: group,
                        exam: o.getAttribute('data-exam') || '',
                        term: o.getAttribute('data-term') || '',
                        year: o.getAttribute('data-year') || ''
                    });
                });
            });
        } else {
            Array.prototype.forEach.call(examSel.options, function (o) {
                pool.push({
                    value: o.value,
                    text: (o.textContent || '').trim(),
                    group: o.getAttribute('data-group') || '',
                    exam: o.getAttribute('data-exam') || '',
                    term: o.getAttribute('data-term') || '',
                    year: o.getAttribute('data-year') || ''
                });
            });
        }
        return pool;
    }

    function render(examSel, entries, selectedValue) {
        var frag = document.createDocumentFragment();
        entries.forEach(function (e) {
            var o = document.createElement('option');
            o.value = e.value;
            o.textContent = e.text;
            o.setAttribute('data-group', e.group);
            o.setAttribute('data-exam', e.exam);
            o.setAttribute('data-term', e.term);
            o.setAttribute('data-year', e.year);
            if (e.value === selectedValue) { o.selected = true; }
            frag.appendChild(o);
        });
        examSel.innerHTML = '';
        examSel.appendChild(frag);
        if (selectedValue) { examSel.value = selectedValue; }
    }

    function init(form) {
        var classSel = form.querySelector('select[data-cascade-role="class"]');
        var examSel = form.querySelector('select[data-cascade-role="exam"]');
        if (!examSel) { return; }
        if (!classSel) { return; } /* single class: server markup is already correct */

        var pool = readPool(examSel);
        if (!pool.length) { return; }

        /* Point the class select at the context the server pre-selected. */
        var currentOpt = examSel.options[examSel.selectedIndex];
        var currentGroup = (currentOpt && currentOpt.getAttribute('data-group')) || pool[0].group;
        classSel.value = currentGroup;
        if (classSel.selectedIndex < 0) { classSel.selectedIndex = 0; }
        var activeGroup = classSel.options[classSel.selectedIndex].value;

        var filtered = pool.filter(function (e) { return e.group === activeGroup; });
        if (!filtered.length) {
            filtered = pool.slice();
            activeGroup = filtered[0].group;
            classSel.value = activeGroup;
        }
        render(examSel, filtered, currentOpt ? currentOpt.value : filtered[0].value);

        classSel.addEventListener('change', function () {
            var newGroup = classSel.value;
            var cur = examSel.options[examSel.selectedIndex];
            var curExam = cur ? cur.getAttribute('data-exam') : '';
            var curTerm = cur ? cur.getAttribute('data-term') : '';
            var curYear = cur ? cur.getAttribute('data-year') : '';

            var entries = pool.filter(function (e) { return e.group === newGroup; });
            if (!entries.length) { return; }

            /* Same assessment in the new class wins; otherwise the newest. */
            var target = null;
            for (var i = 0; i < entries.length; i++) {
                if (entries[i].exam === curExam &&
                    entries[i].term === curTerm &&
                    entries[i].year === curYear) {
                    target = entries[i];
                    break;
                }
            }
            if (!target) { target = entries[0]; }

            render(examSel, entries, target.value);
            form.submit();
        });
    }

    function boot() {
        var forms = document.querySelectorAll('form[data-context-cascade]');
        Array.prototype.forEach.call(forms, init);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
})();
