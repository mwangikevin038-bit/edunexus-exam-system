/**
 * EDUNEXUS Premium Print System - BULLETPROOF v4
 *
 * v4 fixes over v3:
 *   - Double-done() bug: handled flag prevents double-done
 *   - Safety timer: fires onComplete (not retry)
 *   - Retry: failed prints retry up to MAX_RETRY times
 *   - stripComments: iterative TreeWalker (no stack overflow)
 *   - Debounce: cleared timers removed from tracking
 *   - Queue limit: max 10 pending jobs
 */
(function(root) {
    'use strict';

    var SPOOLER_COOLDOWN_MS = 800;
    var DEBOUNCE_MS = 600;
    var CLOSE_DELAY_MS = 2000;
    var MAX_RETRY = 2;
    var MAX_QUEUE = 10;
    var SAFETY_TIMEOUT_MS = 15000;

    var _printBusy = false;
    var _lastPrintTime = 0;
    var _debounceTimer = null;
    var _printQueue = [];
    var _activeTimers = [];

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

    function _trackTimer(id) { _activeTimers.push(id); }

    function _removeTimer(id) {
        var idx = _activeTimers.indexOf(id);
        if (idx !== -1) _activeTimers.splice(idx, 1);
    }

    function _clearAllTimers() {
        for (var i = 0; i < _activeTimers.length; i++) {
            clearTimeout(_activeTimers[i]);
            clearInterval(_activeTimers[i]);
        }
        _activeTimers = [];
        _printQueue = [];
        _printBusy = false;
    }

    if (root.addEventListener) {
        root.addEventListener('beforeunload', _clearAllTimers);
    }

    function debounce(fn) {
        if (_debounceTimer) {
            clearTimeout(_debounceTimer);
            _removeTimer(_debounceTimer);
            _debounceTimer = null;
        }
        _debounceTimer = setTimeout(function() {
            _removeTimer(_debounceTimer);
            _debounceTimer = null;
            fn();
        }, DEBOUNCE_MS);
        _trackTimer(_debounceTimer);
    }

    function enqueuePrint(fn) {
        if (_printQueue.length >= MAX_QUEUE) {
            _printQueue.shift();
        }
        _printQueue.push({ fn: fn, retries: 0 });
        if (!_printBusy) _processQueue();
    }

    function _processQueue() {
        if (_printBusy || _printQueue.length === 0) return;
        _printBusy = true;
        var job = _printQueue.shift();
        var elapsed = Date.now() - _lastPrintTime;
        var delay = elapsed < SPOOLER_COOLDOWN_MS ? (SPOOLER_COOLDOWN_MS - elapsed) : 0;
        var timer = setTimeout(function() {
            _removeTimer(timer);
            _executeJob(job);
        }, delay);
        _trackTimer(timer);
    }

    function _executeJob(job) {
        var completed = false;
        function done() {
            if (completed) return;
            completed = true;
            _lastPrintTime = Date.now();
            _printBusy = false;
            _processQueue();
        }
        function retry() {
            if (job.retries < MAX_RETRY) {
                job.retries++;
                _printQueue.unshift(job);
                _printBusy = false;
                _processQueue();
            } else {
                done();
            }
        }
        try {
            job.fn(done, retry);
        } catch(e) {
            retry();
        }
    }

    function optimizeForPrint(container) {
        var clone = container.cloneNode(true);

        var scripts = clone.querySelectorAll('script');
        for (var i = scripts.length - 1; i >= 0; i--) {
            var s = scripts[i];
            var type = (s.getAttribute('type') || '').toLowerCase();
            var src = (s.getAttribute('src') || '').toLowerCase();
            var isChartPayload = type === 'application/json' && s.id && s.id.indexOf('rc-chart-payload-') === 0;
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

        var firstCard = container.querySelector('.report-card');
        var sectionAccent = '#0f1727';
        if (firstCard) {
            try {
                var v = getComputedStyle(firstCard).getPropertyValue('--section-accent');
                if (v && v.trim()) sectionAccent = v.trim();
            } catch(e) {}
        }

        var optimized;
        try { optimized = optimizeForPrint(container); } catch(e) { retry(); return; }

        var printCSS = _buildPrintCSS(sectionAccent);
        var printWin = _openWindow(750, 650, 20, 50);
        if (!printWin) { _doPrintCurrentPage(opts, done, retry); return; }

        var handled = false;
        function onComplete() {
            if (handled) return;
            handled = true;
            var t = setTimeout(function() { _removeTimer(t); _safeClose(printWin); done(); }, CLOSE_DELAY_MS);
            _trackTimer(t);
        }

        function firePrint() {
            try { printWin.focus(); printWin.print(); } catch(e) { _safeClose(printWin); retry(); return; }
            _listenAfterPrint(printWin, onComplete);
            var st = setTimeout(function() { _removeTimer(st); onComplete(); }, SAFETY_TIMEOUT_MS);
            _trackTimer(st);
        }

        try {
            var doc = printWin.document;
            doc.open();
            doc.write('<!DOCTYPE html><html><head><meta charset="UTF-8"><title>' + escapeHtml(title) + '</title>' +
                '<style>' + printCSS + '</style>' +
                '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"><\/script>' +
                '</head><body>' + optimized.innerHTML + '</body></html>');
            doc.close();

            var checkCount = 0;
            var maxChecks = 60;
            var iv = setInterval(function() {
                checkCount++;
                try {
                    var w = printWin;
                    if (!w || w.closed) { clearInterval(iv); _removeTimer(iv); done(); return; }
                    var chartsReady = w.Chart && w.document.querySelectorAll('canvas').length > 0;
                    var scriptsDone = w.document.readyState === 'complete';
                    if ((chartsReady && scriptsDone) || checkCount >= maxChecks) {
                        clearInterval(iv); _removeTimer(iv);
                        var dt = setTimeout(function() { _removeTimer(dt); firePrint(); }, 600);
                        _trackTimer(dt);
                    }
                } catch(e) {
                    if (checkCount >= maxChecks) {
                        clearInterval(iv); _removeTimer(iv);
                        var dt2 = setTimeout(function() { _removeTimer(dt2); firePrint(); }, 400);
                        _trackTimer(dt2);
                    }
                }
            }, 150);
            _trackTimer(iv);
        } catch(e) { _safeClose(printWin); retry(); }
    }

    function printCurrentPage(opts) {
        debounce(function() {
            enqueuePrint(function(done, retry) { _doPrintCurrentPage(opts, done, retry); });
        });
    }

    function _doPrintCurrentPage(opts, done, retry) {
        opts = opts || {};
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
            var st = setTimeout(function() { _removeTimer(st); onComplete(); }, SAFETY_TIMEOUT_MS);
            _trackTimer(st);
        } catch(e) {
            for (var i = 0; i < saved.length; i++) { try { saved[i].el.style.cssText = saved[i].css; } catch(e2) {} }
            retry();
        }
    }

    function printUrl(url, opts) {
        debounce(function() {
            enqueuePrint(function(done, retry) { _doPrintUrl(url, opts, done, retry); });
        });
    }

    function _doPrintUrl(url, opts, done, retry) {
        opts = opts || {};
        var popup = root.open(url, '_blank', 'width=900,height=700,scrollbars=yes,resizable=yes');
        if (!popup) { _doPrintCurrentPage(opts, done, retry); return; }
        try { popup.document.title = opts.title || 'EDUNEXUS Print'; } catch(e) {}

        var checks = 0;
        var iv = setInterval(function() {
            checks++;
            try {
                if (popup.closed || checks >= 30) { clearInterval(iv); _removeTimer(iv); done(); return; }
                if (popup.document && popup.document.readyState === 'complete') {
                    clearInterval(iv); _removeTimer(iv);
                    var handled = false;
                    _listenAfterPrint(popup, function() {
                        if (handled) return; handled = true;
                        var t = setTimeout(function() { _removeTimer(t); _safeClose(popup); done(); }, CLOSE_DELAY_MS);
                        _trackTimer(t);
                    });
                    var st = setTimeout(function() { _removeTimer(st); if (!handled) { handled = true; _safeClose(popup); done(); } }, SAFETY_TIMEOUT_MS);
                    _trackTimer(st);
                }
            } catch(e) {}
        }, 500);
        _trackTimer(iv);
    }

    function printHtml(html, opts) {
        debounce(function() {
            enqueuePrint(function(done, retry) { _doPrintHtml(html, opts, done, retry); });
        });
    }

    function _doPrintHtml(html, opts, done, retry) {
        opts = opts || {};
        var popup = root.open('', '_blank', 'width=900,height=700,scrollbars=yes,resizable=yes');
        if (!popup) { alert('Please allow popups for this site.'); done(); return; }
        try {
            var doc = popup.document;
            doc.open();
            doc.write('<!DOCTYPE html><html><head><meta charset="UTF-8"><title>' + escapeHtml(opts.title || 'Print') + '</title>');
            if (opts.cssUrl) doc.write('<link rel="stylesheet" href="' + opts.cssUrl + '">');
            doc.write('</head><body>' + html + '</body></html>');
            doc.close();
        } catch(e) { _safeClose(popup); done(); return; }

        var handled = false;
        var pt = setTimeout(function() {
            _removeTimer(pt);
            if (handled) return;
            handled = true;
            try { popup.focus(); popup.print(); } catch(e) {}
            _listenAfterPrint(popup, function() {
                var t = setTimeout(function() { _removeTimer(t); _safeClose(popup); done(); }, CLOSE_DELAY_MS);
                _trackTimer(t);
            });
        }, 1200);
        _trackTimer(pt);
    }

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

    function createProgressOverlay(title) {
        var d = document.createElement('div');
        d.innerHTML = '<div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:99999;display:flex;align-items:center;justify-content:center;"><div style="background:#fff;border-radius:16px;padding:32px 40px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,0.3);max-width:420px;width:90%;"><div style="width:48px;height:48px;border:4px solid #e5e7eb;border-top-color:#4CAF50;border-radius:50%;animation:edunexus-spin 0.8s linear infinite;margin:0 auto 16px;"></div><h3 style="margin:0 0 8px;font-size:16px;font-weight:700;color:#0f172a;">' + escapeHtml(title) + '</h3><p style="margin:0;font-size:13px;color:#64748b;">Preparing your document...</p></div></div><style>@keyframes edunexus-spin{to{transform:rotate(360deg)}}</style>';
        return d;
    }

    function removeProgressOverlay(o) { if (o && o.parentNode) o.parentNode.removeChild(o); }

    function _openWindow(w, h, left, top) {
        return root.open('', '_blank', 'width=' + w + ',height=' + h + ',left=' + left + ',top=' + top + ',scrollbars=yes,resizable=yes,menubar=no,toolbar=no,location=no,status=no');
    }

    function _safeClose(win) { try { if (win && !win.closed) win.close(); } catch(e) {} }

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

    function escapeHtml(s) { var d = document.createElement('div'); d.appendChild(document.createTextNode(s)); return d.innerHTML; }

    function _buildPrintCSS(accent) {
        return '@page{size:A4 portrait;margin:5mm 5mm 8mm 5mm}' +
        '@page{@bottom-center{content:"GENERATED FROM EDUNEXUS EXAM SYSTEM \\00a9 2026";font-family:"Times New Roman",Times,serif;font-size:7pt;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.4pt;border-top:.5pt solid #cbd5e1;width:70%;padding-top:3pt}@bottom-right{content:none}}' +
        '*{-webkit-print-color-adjust:exact!important;print-color-adjust:exact!important;color-adjust:exact!important;box-sizing:border-box}' +
        'html,body{margin:0!important;padding:0!important;background:#fff!important;font-family:"Times New Roman",Times,serif!important;font-size:11pt!important;color:#1C1C1E!important;width:100%!important}' +
        '.app-content,.main-content,.container-fluid{margin:0!important;padding:0!important;max-width:none!important;width:100%!important}' +
        '#reportCardsContainer{display:block!important;width:100%!important;margin:0!important;padding:0!important}' +
        '.rc-card-scroll{overflow:visible!important;margin:0!important;padding:0!important}' +
        '.rc-card-scroll::after{display:none!important}' +
        '.report-card{width:100%!important;max-width:none!important;margin:0!important;padding:.18in .35in .20in .55in!important;border:none!important;border-radius:0!important;box-shadow:none!important;position:relative!important;font-family:"Times New Roman",Times,serif!important;font-size:11pt!important;color:#1C1C1E!important;display:flex!important;flex-direction:column!important;box-sizing:border-box!important;flex-shrink:0!important;page-break-inside:avoid!important;break-inside:avoid!important}' +
        '.report-card::before{content:""!important;display:block!important;position:absolute!important;left:14px!important;top:0!important;bottom:0!important;width:19px!important;height:auto!important;background:' + accent + '!important;border-radius:0 20px 20px 0!important;z-index:1!important}' +
        '.rc-card-scroll+.rc-card-scroll,.rv-card-container+.rv-card-container{page-break-before:always!important}' +
        '.report-content{width:100%!important;margin:0!important;padding:0!important;display:flex!important;flex-direction:column!important;flex:1!important;gap:6px!important}' +
        '.rc-action-buttons,.rc-info-bar,.rc-context-card,.rc-loading-overlay,.rc-topbar,.rc-form-panel,.rc-toolbar,.no-print,.system-footer,.rc-print-watermark,.sub-nav-bar,.sidebar,.global-header,.mobile-global-header,.bottom-nav,.hamburger-btn,.mobile-menu-sheet,.mobile-menu-panel,.mobile-menu-body,.mobile-menu-header,.mobile-menu-backdrop,.rc-descriptors+.system-footer,nav,header{display:none!important;visibility:hidden!important;height:0!important;overflow:hidden!important}' +
        '.rc-header{display:flex!important;align-items:center!important;gap:14px!important;padding-bottom:5px!important;margin-bottom:4px!important;position:relative!important;border-bottom:none!important;width:100%!important}' +
        '.rc-header::after{display:none!important}' +
        '.report-logo,.rc-logo-placeholder{width:80px!important;height:80px!important;border-radius:8px!important;object-fit:contain!important;flex-shrink:0!important}' +
        '.rc-logo-spacer{width:80px!important;flex-shrink:0!important}' +
        '.rc-schoolinfo{flex:1!important;text-align:center!important}' +
        '.rc-schoolinfo h1{font-family:"Times New Roman",Times,serif!important;font-size:16pt!important;font-weight:900!important;margin:0 0 4px!important;text-transform:uppercase!important;color:' + accent + '!important;letter-spacing:0.04em!important;line-height:1.1!important}' +
        '.rc-schoolinfo .rc-address{font-family:"Times New Roman",Times,serif!important;font-size:10pt!important;font-weight:600!important;color:#1C1C1E!important;margin-bottom:2px!important}' +
        '.rc-schoolinfo .rc-contact-line{font-family:"Times New Roman",Times,serif!important;font-size:10pt!important;color:#444!important;font-weight:500!important;margin:0!important}' +
        '.rc-banner{background:' + accent + '!important;color:#fff!important;text-align:center!important;font-size:10pt!important;font-weight:800!important;letter-spacing:0.04em!important;text-transform:uppercase!important;padding:5px 14px!important;border-radius:0!important;white-space:nowrap!important;overflow:hidden!important;text-overflow:ellipsis!important;margin-left:calc(-0.55in + 33px)!important;width:auto!important;margin-bottom:0!important}' +
        '.rc-top-grid{display:flex!important;flex-direction:row!important;justify-content:space-between!important;align-items:stretch!important;gap:20px!important;width:100%!important;overflow:hidden!important}' +
        '.rc-student-block{flex:1!important;min-width:200px!important;display:flex!important;gap:14px!important;align-items:flex-start!important}' +
        '.rc-initials-avatar{width:80px!important;height:90px!important;border-radius:8px!important;flex-shrink:0!important;background:' + accent + '!important;color:#fff!important;display:flex!important;align-items:center!important;justify-content:center!important;font-family:"Times New Roman",Times,serif!important;font-size:28pt!important;font-weight:800!important;letter-spacing:0.02em!important;user-select:none!important}' +
        '.rc-student-info{flex:1!important}' +
        '.rc-student-name{font-family:"Times New Roman",Times,serif!important;font-size:13pt!important;font-weight:800!important;margin-bottom:6px!important;color:#1C1C1E!important}' +
        '.rc-detail{font-family:"Times New Roman",Times,serif!important;font-size:11pt!important;margin-bottom:3px!important;color:#1C1C1E!important;line-height:1.4!important}' +
        '.rc-label{font-weight:800!important;margin-right:2px!important;text-transform:uppercase!important;color:#4CAF50!important}' +
        '.rc-chart-block{flex:2!important;padding:0!important;margin:0!important;display:flex!important;flex-direction:column!important;position:relative!important;min-height:0!important;max-height:48mm!important;overflow:hidden!important}' +
        '.rc-chart-title{font-family:"Times New Roman",Times,serif!important;font-size:9pt!important;font-weight:700!important;color:#64748B!important;text-transform:uppercase!important;letter-spacing:0.04em!important;margin-bottom:3px!important;padding-bottom:0!important;text-align:left!important;line-height:1!important}' +
        '.rc-chart-canvas-wrap{height:auto!important;max-height:44mm!important;overflow:hidden!important;position:relative!important;z-index:2!important}' +
        '.rc-chart-canvas-wrap canvas{display:block!important;width:100%!important;max-height:44mm!important}' +
        '.rc-chart-canvas-wrap img{display:block!important;width:100%!important;max-height:44mm!important;object-fit:contain!important;margin:0 auto!important}' +
        '.rc-chart-svg{width:100%!important;margin:6px 0!important;overflow:hidden!important}' +
        '.rc-chart-svg svg{width:100%!important;height:auto!important;max-height:44mm!important;display:block!important}' +
        '.rc-chart-img{width:100%!important;max-height:44mm!important;object-fit:contain!important}' +
        '.rc-stats{display:grid!important;grid-template-columns:repeat(5,1fr)!important;gap:8px!important;width:100%!important}' +
        '.rc-stat{border:1px solid #E2E8F0!important;border-top:3px solid ' + accent + '!important;border-radius:10px!important;padding:12px 10px!important;text-align:center!important;background:linear-gradient(180deg,#FFFFFF 0%,#F8FAFC 100%)!important}' +
        '.rc-stat-label{display:block!important;font-family:"Times New Roman",Times,serif!important;font-size:8pt!important;font-weight:800!important;color:' + accent + '!important;text-transform:uppercase!important;letter-spacing:0.06em!important;margin-bottom:6px!important}' +
        '.rc-stat-value{display:block!important;font-family:"Times New Roman",Times,serif!important;font-size:15pt!important;font-weight:800!important;color:#1E293B!important;line-height:1.2!important}' +
        '.rc-stat-value small{font-size:10pt!important;font-weight:600!important;color:#94A3B8!important}' +
        '.table-scroll{width:100%!important;overflow:visible!important}' +
        '.rc-table{width:100%!important;border-collapse:collapse!important;min-width:0!important;table-layout:fixed!important}' +
        '.rc-table colgroup .col-subj{width:32%!important}' +
        '.rc-table colgroup .col-marks{width:13%!important}' +
        '.rc-table colgroup .col-dev{width:18%!important}' +
        '.rc-table colgroup .col-grade{width:12%!important}' +
        '.rc-table colgroup .col-teacher{width:25%!important}' +
        '.rc-table thead th{background:#E9ECF0!important;color:#1E293B!important;font-family:"Times New Roman",Times,serif!important;font-size:9pt!important;font-weight:800!important;text-transform:uppercase!important;letter-spacing:0.03em!important;padding:6px 8px!important;border:1px solid #000!important;white-space:nowrap!important}' +
        '.rc-table thead th:first-child{text-align:left!important}' +
        '.rc-table tbody tr{page-break-inside:avoid!important}' +
        '.rc-table tbody td{font-family:"Times New Roman",Times,serif!important;padding:5px 8px!important;border:1px solid #000!important;font-size:11pt!important;color:#1C1C1E!important;vertical-align:middle!important}' +
        '.rc-subj{font-weight:700!important;text-align:left!important;white-space:nowrap!important}' +
        '.rc-center{text-align:center!important}' +
        '.rc-grade{font-weight:800!important;color:#2563EB!important}' +
        '.rc-teacher{font-style:italic!important;font-weight:600!important;padding-left:10px!important;text-align:left!important;font-size:11pt!important}' +
        '.rc-comments-header{background:' + accent + '!important;color:#fff!important;font-size:10pt!important;font-weight:800!important;text-transform:uppercase!important;letter-spacing:0.06em!important;text-align:center!important;padding:5px 14px!important;border-radius:0 20px 0 0!important;margin-left:-20px!important;margin-right:20px!important;box-decoration-break:clone!important;-webkit-box-decoration-break:clone!important}' +
        '.rc-remarks-grid{display:grid!important;grid-template-columns:1fr 1fr!important;gap:14px!important;padding:14px!important;background:#F8FAFC!important;border-radius:0 0 8px 8px!important;margin-left:-20px!important;margin-right:20px!important}' +
        '.rc-remark-box{border:1px solid #E2E8F0!important;border-radius:10px!important;padding:14px 16px!important;background:#fff!important;display:flex!important;flex-direction:column!important}' +
        '.rc-remark-title{font-family:"Times New Roman",Times,serif!important;font-size:11pt!important;font-weight:800!important;color:#1E293B!important;margin-bottom:10px!important;padding-bottom:0!important;border-bottom:none!important}' +
        '.rc-remark-text{font-family:"Times New Roman",Times,serif!important;font-size:11pt!important;font-weight:400!important;color:#334155!important;line-height:1.5!important;margin-bottom:10px!important;flex-grow:1!important}' +
        '.rc-signature{font-family:"Times New Roman",Times,serif!important;font-size:9pt!important;font-weight:600!important;color:#64748B!important;border-top:1px dashed #CBD5E1!important;padding-top:6px!important;margin-top:auto!important}' +
        '.rc-descriptors{margin-top:10px!important;width:100%!important}' +
        '.rc-descriptors-title{font-family:"Times New Roman",Times,serif!important;font-size:9pt!important;font-weight:800!important;color:#1E293B!important;text-transform:uppercase!important;letter-spacing:0.05em!important;margin-bottom:4px!important;padding-bottom:4px!important;border-bottom:2px solid ' + accent + '!important}' +
        '.rc-descriptors-table{width:100%!important;border-collapse:collapse!important;border-radius:8px!important;overflow:hidden!important}' +
        '.rc-descriptors-table th,.rc-descriptors-table td{border:1px solid #000!important;padding:5px 8px!important;font-size:9pt!important;text-align:center!important;font-family:"Times New Roman",Times,serif!important}' +
        '.rc-descriptors-table th{background:#E9ECF0!important;color:#1E293B!important;font-weight:800!important;text-transform:uppercase!important;letter-spacing:0.03em!important}' +
        '.rc-descriptors-table td:first-child,.rc-descriptors-table th:first-child{text-align:left!important;font-weight:700!important;background:#F8FAFC!important;color:#1E293B!important}' +
        '.footer-dates{margin-top:14px!important;display:grid!important;grid-template-columns:1fr 1fr!important;gap:16px!important;width:100%!important}' +
        '.date-box{display:flex!important;justify-content:space-between!important;align-items:center!important;font-family:"Times New Roman",Times,serif!important;font-size:11pt!important;font-weight:700!important;color:#1E293B!important;padding:10px 14px!important;border-radius:8px!important;border:1px solid #E2E8F0!important;background:linear-gradient(180deg,#FFFFFF 0%,#F8FAFC 100%)!important}' +
        '.date-box span:first-child{color:#64748B!important;text-transform:uppercase!important;letter-spacing:0.04em!important;font-size:9pt!important}' +
        '.footer-dates+.system-footer,.footer-dates+.rc-print-watermark{display:none!important}' +
        '*{transition:none!important;animation:none!important}';
    }

    root.EDUNEXUSPrint = {
        printUrl: printUrl,
        printCurrentPage: printCurrentPage,
        printHtml: printHtml,
        openPrintWindow: openPrintWindow,
        backgroundPdf: backgroundPdf,
        createProgressOverlay: createProgressOverlay,
        removeProgressOverlay: removeProgressOverlay
    };

})(window);
