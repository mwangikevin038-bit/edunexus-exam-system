/**
 * EDUNEXUS Premium Print System - BULLETPROOF v5
 *
 * v5 architectural changes:
 *   - Named popup window: reuses single window handle, no zombie windows
 *   - Hard safety timer: _printBusy force-clears after HARD_SAFETY_MS (25s)
 *   - Popup close detection: polling detects user-closed popup, releases lock
 *   - Simplified queue: sequential jobs, one at a time, always recovers
 *   - No permanent lock: system ALWAYS returns to IDLE state
 */
(function(root) {
    'use strict';

    /* ── Constants ─────────────────────────────────────────────────────── */
    var DEBOUNCE_MS        = 500;
    var COOLDOWN_MS        = 800;
    var CLOSE_DELAY_MS     = 1500;
    var MAX_RETRY          = 2;
    var MAX_QUEUE          = 10;
    var SAFETY_TIMEOUT_MS  = 15000;
    var HARD_SAFETY_MS     = 25000;
    var CLOSE_POLL_MS      = 1000;
    var POPUP_NAME         = 'edunexus_print_popup';
    var POPUP_FEATURES     = 'width=900,height=700,left=50,top=50,scrollbars=yes,resizable=yes,menubar=no,toolbar=no,location=no,status=no';

    /* ── State ─────────────────────────────────────────────────────────── */
    var _printBusy     = false;
    var _busySince     = 0;
    var _lastPrintTime = 0;
    var _debounceTimer = null;
    var _printQueue    = [];
    var _activeTimers  = [];
    var _popupRef      = null;
    var _closePoller   = null;
    var _hardSafety    = null;

    /* ── Chrome selectors: elements to hide during inline fallback ────── */
    var CHROME_SELECTORS = [
        '.sidebar', '.premium-sidebar', '.sidebar-overlay',
        '.bottom-nav', '.mobile-topbar', '.global-header',
        '.app-header', '.cp-mobile-header', '.hamburger-btn',
        '.mobile-menu-sheet', '.mobile-menu-panel', '.mobile-menu-body',
        '.mobile-menu-header', '.mobile-menu-backdrop',
        '.system-footer', '.sub-nav-bar',
        '.rc-topbar', '.rc-action-buttons', '.rc-info-bar', '.rc-context-card',
        '.rc-loading-overlay', '.rc-form-panel', '.rc-toolbar',
        '.no-print', '.print-watermark', '.rc-print-watermark',
        '#globalLoaderOverlay'
    ].join(',');

    /* ── Timer tracking ────────────────────────────────────────────────── */
    function _track(id) { _activeTimers.push(id); }
    function _untrack(id) {
        var i = _activeTimers.indexOf(id);
        if (i !== -1) _activeTimers.splice(i, 1);
    }
    function _clearAllTimers() {
        for (var i = 0; i < _activeTimers.length; i++) {
            clearTimeout(_activeTimers[i]);
            clearInterval(_activeTimers[i]);
        }
        _activeTimers = [];
        _printQueue = [];
        _releaseLock();
    }

    if (root.addEventListener) {
        root.addEventListener('beforeunload', _clearAllTimers);
    }

    /* ── Lock management ───────────────────────────────────────────────── */
    function _acquireLock() {
        _printBusy = true;
        _busySince = Date.now();
        _startHardSafety();
        _startClosePoller();
    }

    function _releaseLock() {
        _printBusy = false;
        _busySince = 0;
        _popupRef = null;
        _stopHardSafety();
        _stopClosePoller();
    }

    function _startHardSafety() {
        _stopHardSafety();
        _hardSafety = setTimeout(function() {
            _hardSafety = null;
            if (!_printBusy) return;
            _safeClose(_popupRef);
            _releaseLock();
            _processQueue();
        }, HARD_SAFETY_MS);
        _track(_hardSafety);
    }

    function _stopHardSafety() {
        if (_hardSafety) { clearTimeout(_hardSafety); _untrack(_hardSafety); _hardSafety = null; }
    }

    function _startClosePoller() {
        _stopClosePoller();
        _closePoller = setInterval(function() {
            if (!_popupRef) { _stopClosePoller(); return; }
            try {
                if (_popupRef.closed) {
                    _stopClosePoller();
                    _releaseLock();
                    _processQueue();
                }
            } catch(e) {
                _stopClosePoller();
                _releaseLock();
                _processQueue();
            }
        }, CLOSE_POLL_MS);
        _track(_closePoller);
    }

    function _stopClosePoller() {
        if (_closePoller) { clearInterval(_closePoller); _untrack(_closePoller); _closePoller = null; }
    }

    /* ── Queue ─────────────────────────────────────────────────────────── */
    function enqueuePrint(fn) {
        if (_printQueue.length >= MAX_QUEUE) _printQueue.shift();
        _printQueue.push({ fn: fn, retries: 0 });
        if (!_printBusy) _processQueue();
    }

    function _processQueue() {
        if (_printBusy || _printQueue.length === 0) return;
        var job = _printQueue.shift();
        var elapsed = Date.now() - _lastPrintTime;
        var delay = elapsed < COOLDOWN_MS ? (COOLDOWN_MS - elapsed) : 0;
        var t = setTimeout(function() {
            _untrack(t);
            _executeJob(job);
        }, delay);
        _track(t);
    }

    function _executeJob(job) {
        var done = false;
        function onComplete() {
            if (done) return;
            done = true;
            _lastPrintTime = Date.now();
            _releaseLock();
            _processQueue();
        }
        function onRetry() {
            if (done) return;
            done = true;
            if (job.retries < MAX_RETRY) {
                job.retries++;
                _printQueue.unshift(job);
                _releaseLock();
                _processQueue();
            } else {
                onComplete();
            }
        }
        _acquireLock();
        try {
            job.fn(onComplete, onRetry);
        } catch(e) {
            onRetry();
        }
    }

    /* ── Debounce wrapper ──────────────────────────────────────────────── */
    function debounce(fn) {
        if (_debounceTimer) { clearTimeout(_debounceTimer); _untrack(_debounceTimer); }
        _debounceTimer = setTimeout(function() {
            _untrack(_debounceTimer);
            _debounceTimer = null;
            fn();
        }, DEBOUNCE_MS);
        _track(_debounceTimer);
    }

    /* ── DOM optimization for print ────────────────────────────────────── */
    function optimizeForPrint(container) {
        var clone = container.cloneNode(true);

        var scripts = clone.querySelectorAll('script');
        for (var i = scripts.length - 1; i >= 0; i--) {
            var s = scripts[i];
            var type = (s.getAttribute('type') || '').toLowerCase();
            var src = (s.getAttribute('src') || '').toLowerCase();
            var isChartPayload = type === 'application/json' && s.id && (s.id.indexOf('rc-chart-payload-') === 0 || s.id.indexOf('ar-chart-payload') === 0);
            var isChartInit = !src && s.textContent && s.textContent.indexOf('new Chart(') !== -1;
            var isChartUid = !src && s.textContent && s.textContent.indexOf('rc-chart-payload-') !== -1;
            if (isChartPayload || isChartInit || isChartUid) { continue; }
            s.remove();
        }

        var walker = document.createTreeWalker(clone, NodeFilter.SHOW_COMMENT, null, false);
        var comments = [];
        while (walker.nextNode()) comments.push(walker.currentNode);
        for (var i = 0; i < comments.length; i++) {
            if (comments[i].parentNode) comments[i].parentNode.removeChild(comments[i]);
        }
        var hidden = clone.querySelectorAll('[hidden]');
        for (var i = 0; i < hidden.length; i++) hidden[i].remove();
        var allEls = clone.querySelectorAll('*');
        for (var i = 0; i < allEls.length; i++) {
            var el = allEls[i];
            var attrs = el.attributes;
            for (var j = attrs.length - 1; j >= 0; j--) {
                if (attrs[j].name.toLowerCase().indexOf('on') === 0) {
                    el.removeAttribute(attrs[j].name);
                }
            }
        }
        return clone;
    }

    /* ── Popup window ──────────────────────────────────────────────────── */
    function _openPopup() {
        try {
            var win = root.open('', POPUP_NAME, POPUP_FEATURES);
            if (win) _popupRef = win;
            return win;
        } catch(e) { return null; }
    }

    function _safeClose(win) {
        try { if (win && !win.closed) win.close(); } catch(e) {}
    }

    /* ── afterprint listener (dual event) ──────────────────────────────── */
    function _listenAfterPrint(win, callback) {
        var called = false;
        function once() { if (called) return; called = true; callback(); }
        try { win.addEventListener('afterprint', once); } catch(e) {}
        try {
            var mql = win.matchMedia('print');
            if (mql && mql.addEventListener) {
                mql.addEventListener('change', function(e) { if (!e.matches) once(); });
            } else if (mql && mql.addListener) {
                mql.addListener(function(e) { if (!e.matches) once(); });
            }
        } catch(e) {}
    }

    /* ── Helpers ───────────────────────────────────────────────────────── */
    function escapeHtml(s) {
        var d = document.createElement('div');
        d.appendChild(document.createTextNode(s));
        return d.innerHTML;
    }

    /* ══════════════════════════════════════════════════════════════════════
       PUBLIC: openPrintWindow(selector, opts)
       Clone DOM → write to named popup → inject CSS → print → close
       ══════════════════════════════════════════════════════════════════════ */
    function openPrintWindow(selector, opts) {
        debounce(function() {
            enqueuePrint(function(done, retry) {
                _doOpenPrintWindow(selector, opts, done, retry);
            });
        });
    }

    function _doOpenPrintWindow(selector, opts, done, retry) {
        opts = opts || {};
        var title = opts.title || 'EDUNEXUS Print';
        var container = document.querySelector(selector);
        if (!container) { done(); return; }

        var optimized;
        try { optimized = optimizeForPrint(container); } catch(e) { retry(); return; }

        var pageStyle = document.getElementById('pagePrintCSS');
        var printWin = _openPopup();
        if (!printWin) { _doFallbackInline(opts, done, retry); return; }

        var handled = false;
        function onComplete() {
            if (handled) return;
            handled = true;
            var t = setTimeout(function() { _untrack(t); _safeClose(printWin); done(); }, CLOSE_DELAY_MS);
            _track(t);
        }

        function firePrint() {
            try { printWin.focus(); printWin.print(); } catch(e) { _safeClose(printWin); retry(); return; }
            _listenAfterPrint(printWin, onComplete);
            var st = setTimeout(function() { _untrack(st); onComplete(); }, SAFETY_TIMEOUT_MS);
            _track(st);
        }

        function writePopup(sharedCSS, broadsheetCSS) {
            var pageCSS = pageStyle && pageStyle.textContent ? pageStyle.textContent : '';
            var isReportCard =
                selector === '#reportCardsContainer' ||
                selector === '.rv-scroll' ||
                (!!container && !!container.querySelector('.report-card'));

            var templateCSS;
            if (pageCSS) {
                templateCSS = pageCSS;
            } else if (isReportCard) {
                // Report cards must never inherit broadsheet A4 landscape.
                templateCSS = '';
            } else {
                templateCSS = broadsheetCSS;
            }

            var combinedCSS = (sharedCSS || '') + '\n' + (templateCSS || '');

            if (isReportCard) {
                // Force portrait last so any earlier @page size cannot win.
                combinedCSS += [
                    '',
                    '@page { size: A4 portrait !important; margin: 5mm 5mm 8mm 5mm; }',
                    '@page landscape { size: A4 portrait !important; margin: 5mm 5mm 8mm 5mm; }',
                    '@media print {',
                    '  @page { size: A4 portrait !important; margin: 5mm 5mm 8mm 5mm; }',
                    '  .report-card { page-break-inside: avoid !important; break-inside: avoid !important; }',
                    '  .report-card + .report-card { page-break-before: always !important; break-before: page !important; }',
                    '  .rc-descriptors, .rc-descriptors-table, .footer-dates, .rc-remarks-grid {',
                    '    page-break-inside: avoid !important; break-inside: avoid !important;',
                    '  }',
                    '}'
                ].join('\n');
            }

            try {
                var doc = printWin.document;
                doc.open();
                doc.write('<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>' + escapeHtml(title) + '</title>' +
                    '<style>' + combinedCSS + '</style>' +
                    '<script src="/static/js/chart.min.js"><\/script>' +
                    '</head><body>' + optimized.outerHTML +
                    '</body></html>');
                doc.close();
            } catch(e) { _safeClose(printWin); retry(); return; }

            var checkCount = 0;
            var maxChecks = 60;
            var iv = setInterval(function() {
                checkCount++;
                try {
                    var w = printWin;
                    if (!w || w.closed) { clearInterval(iv); _untrack(iv); done(); return; }
                    var hasCanvas = w.document.querySelectorAll('canvas').length > 0;
                    var chartsReady = w.Chart && hasCanvas;
                    var scriptsDone = w.document.readyState === 'complete';
                    var noChartsNeeded = !hasCanvas && scriptsDone;
                    if ((chartsReady && scriptsDone) || noChartsNeeded || checkCount >= maxChecks) {
                        clearInterval(iv); _untrack(iv);
                        var dt = setTimeout(function() { _untrack(dt); firePrint(); }, 600);
                        _track(dt);
                    }
                } catch(e) {
                    if (checkCount >= maxChecks) {
                        clearInterval(iv); _untrack(iv);
                        var dt2 = setTimeout(function() { _untrack(dt2); firePrint(); }, 400);
                        _track(dt2);
                    }
                }
            }, 150);
            _track(iv);
        }

        var isReportCardPrint =
            selector === '#reportCardsContainer' ||
            selector === '.rv-scroll' ||
            (!!container && !!container.querySelector('.report-card'));

        Promise.all([
            fetch('/static/css/print-shared.css').then(function(r) { return r.ok ? r.text() : ''; }).catch(function() { return ''; }),
            isReportCardPrint
                ? Promise.resolve('')
                : fetch('/static/css/broadsheet.css').then(function(r) { return r.ok ? r.text() : ''; }).catch(function() { return ''; })
        ]).then(function(results) { writePopup(results[0], results[1]); })
          .catch(function() { writePopup('', ''); });
    }

    /* ══════════════════════════════════════════════════════════════════════
       PUBLIC: printCurrentPage(opts)  — fallback only (inline print)
       Used by templates that can't clone a container (e.g. teachers list)
       ══════════════════════════════════════════════════════════════════════ */
    function printCurrentPage(opts) {
        debounce(function() {
            enqueuePrint(function(done, retry) { _doFallbackInline(opts || {}, done, retry); });
        });
    }

    function _doFallbackInline(opts, done, retry) {
        var saved = [];
        try {
            var els = document.querySelectorAll(CHROME_SELECTORS);
            for (var i = 0; i < els.length; i++) {
                saved.push({ el: els[i], css: els[i].style.cssText });
                els[i].style.cssText = 'display:none !important; visibility:hidden !important; height:0 !important; overflow:hidden !important;';
            }
            var ac = document.querySelector('.app-content');
            if (ac) { saved.push({ el: ac, css: ac.style.cssText }); ac.style.cssText = 'margin-top:0 !important;'; }
            var mc = document.querySelector('.main-content');
            if (mc) { saved.push({ el: mc, css: mc.style.cssText }); mc.style.cssText = 'margin-left:0 !important; max-width:100% !important; padding:0 !important;'; }

            root.focus();
            root.print();

            var handled = false;
            function onComplete() {
                if (handled) return;
                handled = true;
                for (var i = 0; i < saved.length; i++) { try { saved[i].el.style.cssText = saved[i].css; } catch(e) {} }
                done();
            }
            _listenAfterPrint(root, onComplete);
            var st = setTimeout(function() { _untrack(st); onComplete(); }, SAFETY_TIMEOUT_MS);
            _track(st);
        } catch(e) {
            for (var i = 0; i < saved.length; i++) { try { saved[i].el.style.cssText = saved[i].css; } catch(e2) {} }
            retry();
        }
    }

    /* ══════════════════════════════════════════════════════════════════════
       PUBLIC: printUrl(url, opts)  — opens URL in named popup, prints
       ══════════════════════════════════════════════════════════════════════ */
    function printUrl(url, opts) {
        debounce(function() {
            enqueuePrint(function(done, retry) { _doPrintUrl(url, opts, done, retry); });
        });
    }

    function _doPrintUrl(url, opts, done, retry) {
        opts = opts || {};
        var popup = root.open(url, POPUP_NAME, POPUP_FEATURES);
        if (!popup) { _doFallbackInline(opts, done, retry); return; }
        _popupRef = popup;
        try { popup.document.title = opts.title || 'EDUNEXUS Print'; } catch(e) {}

        var checks = 0;
        var iv = setInterval(function() {
            checks++;
            try {
                if (popup.closed || checks >= 30) { clearInterval(iv); _untrack(iv); done(); return; }
                if (popup.document && popup.document.readyState === 'complete') {
                    clearInterval(iv); _untrack(iv);
                    var handled = false;
                    _listenAfterPrint(popup, function() {
                        if (handled) return; handled = true;
                        var t = setTimeout(function() { _untrack(t); done(); }, CLOSE_DELAY_MS);
                        _track(t);
                    });
                    var st = setTimeout(function() { _untrack(st); if (!handled) { handled = true; done(); } }, SAFETY_TIMEOUT_MS);
                    _track(st);
                }
            } catch(e) {}
        }, 500);
        _track(iv);
    }

    /* ══════════════════════════════════════════════════════════════════════
       PUBLIC: printHtml(html, opts) — write raw HTML to popup, print
       ══════════════════════════════════════════════════════════════════════ */
    function printHtml(html, opts) {
        debounce(function() {
            enqueuePrint(function(done, retry) { _doPrintHtml(html, opts, done, retry); });
        });
    }

    function _doPrintHtml(html, opts, done, retry) {
        opts = opts || {};
        var popup = root.open('', POPUP_NAME, POPUP_FEATURES);
        if (!popup) { _doFallbackInline(opts, done, retry); return; }
        _popupRef = popup;
        try {
            var doc = popup.document;
            doc.open();
            doc.write('<!DOCTYPE html><html><head><meta charset="UTF-8"><title>' + escapeHtml(opts.title || 'Print') + '</title>');
            if (opts.cssUrl) doc.write('<link rel="stylesheet" href="' + opts.cssUrl + '">');
            doc.write('</head><body>' + html + '</body></html>');
            doc.close();
        } catch(e) { _safeClose(popup); retry(); return; }

        var handled = false;
        var pt = setTimeout(function() {
            _untrack(pt);
            if (handled) return;
            handled = true;
            try { popup.focus(); popup.print(); } catch(e) {}
            _listenAfterPrint(popup, function() {
                var t = setTimeout(function() { _untrack(t); done(); }, CLOSE_DELAY_MS);
                _track(t);
            });
        }, 1200);
        _track(pt);
    }

    /* ══════════════════════════════════════════════════════════════════════
       PUBLIC: backgroundPdf(opts) — server-side PDF via AJAX
       ══════════════════════════════════════════════════════════════════════ */
    function backgroundPdf(opts) {
        opts = opts || {};
        var overlay = createProgressOverlay(opts.title || 'Generating PDF...');
        document.body.appendChild(overlay);
        fetch(opts.url, { method: 'GET', credentials: 'same-origin' })
            .then(function(r) { if (!r.ok) throw new Error('Server error'); return r.blob(); })
            .then(function(blob) {
                removeProgressOverlay(overlay);
                var a = document.createElement('a');
                a.href = URL.createObjectURL(blob);
                a.download = opts.filename || 'document.pdf';
                document.body.appendChild(a);
                a.click();
                setTimeout(function() { URL.revokeObjectURL(a.href); }, 5000);
            })
            .catch(function(e) {
                removeProgressOverlay(overlay);
                alert('PDF generation failed: ' + e.message);
            });
    }

    /* ══════════════════════════════════════════════════════════════════════
       UI UTILITIES
       ══════════════════════════════════════════════════════════════════════ */
    function createProgressOverlay(title) {
        var d = document.createElement('div');
        d.innerHTML = '<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:99999;display:flex;align-items:center;justify-content:center;"><div style="background:#fff;border-radius:16px;padding:32px 40px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.3);max-width:420px;width:90%;"><div style="width:48px;height:48px;border:4px solid #e5e7eb;border-top-color:#4CAF50;border-radius:50%;animation:edunexus-spin 0.8s linear infinite;margin:0 auto 16px;"></div><h3 style="margin:0 0 8px;font-size:16px;font-weight:700;color:#0f172a;">' + escapeHtml(title) + '</h3><p style="margin:0;font-size:13px;color:#64748b;">Preparing your document...</p></div></div><style>@keyframes edunexus-spin{to{transform:rotate(360deg)}}</style>';
        return d;
    }

    function removeProgressOverlay(o) { if (o && o.parentNode) o.parentNode.removeChild(o); }

    /* ══════════════════════════════════════════════════════════════════════
       PUBLIC API
       ══════════════════════════════════════════════════════════════════════ */
    root.EDUNEXUSPrint = {
        openPrintWindow:      openPrintWindow,
        printCurrentPage:     printCurrentPage,
        printUrl:             printUrl,
        printHtml:            printHtml,
        backgroundPdf:        backgroundPdf,
        createProgressOverlay: createProgressOverlay,
        removeProgressOverlay:  removeProgressOverlay
    };

})(window);
