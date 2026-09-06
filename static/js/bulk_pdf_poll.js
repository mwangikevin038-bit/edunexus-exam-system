/**
 * Shared bulk PDF generation logic with WebSocket + polling fallback.
 *
 * Usage from a template:
 *   <div id="rcGenOverlay"
 *        data-start-url="{% url 'start_bulk_report_pdf' %}?..."
 *        data-poll-url="{% url 'pdf_progress' job_id='__JOBID__' %}"
 *        data-download-url="{% url 'download_generated_pdf' job_id='__JOBID__' %}"
 *        data-ws-url="ws://host/ws/pdf-progress/__JOBID__/">
 *   ...
 *
 * Then call: BulkPDF.start(selectedIds, { grade, stream, examId, ... })
 */
var BulkPDF = (function () {
    var _pollTimer = null;
    var _cancelled = false;
    var _ws = null;
    var _jobId = null;

    function _buildStartUrl(base, params, ids) {
        var sep = base.indexOf('?') === -1 ? '?' : '&';
        var parts = [];
        for (var k in params) {
            if (params[k]) parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
        if (ids && ids.length) parts.push('ids=' + ids.join(','));
        return base + sep + parts.join('&');
    }

    function _updateProgress(data, total, progressEl) {
        if (data.status === 'processing') {
            var compiled = data.compiled || 0;
            var failed = data.failed || 0;
            progressEl.textContent = 'Compiling ' + compiled + '/' + total + ' report cards...' + (failed > 0 ? ' (' + failed + ' failed)' : '');
        }
    }

    function _handleComplete(data, overlay, progressEl, cancelBtn, downloadTpl, jobId) {
        var msg = 'Done! ' + (data.compiled || 0) + ' report cards compiled.';
        if (data.failed > 0) msg += ' ' + data.failed + ' failed.';
        progressEl.textContent = msg + ' Downloading...';
        if (cancelBtn) cancelBtn.style.display = 'none';
        var downloadUrl = downloadTpl.replace('__JOBID__', jobId);
        window.location.href = downloadUrl;
        setTimeout(function () { overlay.classList.remove('visible'); }, 2000);
        _cleanupWs();
    }

    function _cleanupWs() {
        if (_ws) { try { _ws.close(); } catch (e) {} _ws = null; }
    }

    function _connectWs(jobId, total, overlay, progressEl, cancelBtn, downloadTpl) {
        var overlayEl = document.getElementById('rcGenOverlay');
        var wsTpl = overlayEl ? overlayEl.getAttribute('data-ws-url') : '';
        if (!wsTpl || typeof WebSocket === 'undefined') return false;

        var wsUrl = wsTpl.replace('__JOBID__', jobId);
        try {
            _ws = new WebSocket(wsUrl);
            _ws.onmessage = function (evt) {
                if (_cancelled) return;
                try {
                    var data = JSON.parse(evt.data);
                    if (data.status === 'processing') {
                        _updateProgress(data, total, progressEl);
                    } else if (data.status === 'completed' || data.status === 'completed_with_errors') {
                        _handleComplete(data, overlay, progressEl, cancelBtn, downloadTpl, jobId);
                    } else if (data.status === 'error') {
                        overlay.classList.remove('visible');
                        alert('PDF generation failed: ' + (data.message || 'Unknown error'));
                        _cleanupWs();
                    }
                } catch (e) {}
            };
            _ws.onclose = function () {
                if (!_cancelled) _poll(jobId, total, overlay, progressEl, cancelBtn, downloadTpl);
            };
            _ws.onerror = function () {
                _cleanupWs();
                if (!_cancelled) _poll(jobId, total, overlay, progressEl, cancelBtn, downloadTpl);
            };
            return true;
        } catch (e) {
            return false;
        }
    }

    function start(selectedIds, params) {
        if (!selectedIds || !selectedIds.length) {
            alert('Please select at least one learner first.');
            return;
        }

        // Pre-estimate: ~100KB per student, warn if likely > 50MB
        var estMB = Math.round(selectedIds.length * 0.1);
        if (estMB > 50) {
            if (!confirm('This will generate approximately ' + estMB + 'MB of PDF data. This may take several minutes.\n\nContinue?')) {
                return;
            }
        }

        var overlay = document.getElementById('rcGenOverlay');
        var progressEl = document.getElementById('rcGenProgress');
        var cancelBtn = document.getElementById('rcGenCancelBtn');
        if (!overlay || !progressEl) return;

        _cancelled = false;
        overlay.classList.add('visible');
        progressEl.textContent = 'Preparing ' + selectedIds.length + ' report cards...';
        if (cancelBtn) cancelBtn.style.display = 'inline-block';

        var startUrl = overlay.getAttribute('data-start-url');
        var pollTpl = overlay.getAttribute('data-poll-url');
        var downloadTpl = overlay.getAttribute('data-download-url');

        var url = _buildStartUrl(startUrl, params, selectedIds);

        fetch(url, { method: 'GET', credentials: 'same-origin' })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (_cancelled) return;
                if (data.error) {
                    overlay.classList.remove('visible');
                    alert('Error: ' + data.error);
                    return;
                }
                var jobId = data.job_id;
                _jobId = jobId;
                var total = data.total || selectedIds.length;
                progressEl.textContent = 'Compiling 0/' + total + ' report cards...';

                if (!_connectWs(jobId, total, overlay, progressEl, cancelBtn, downloadTpl)) {
                    _poll(jobId, total, overlay, progressEl, cancelBtn, pollTpl, downloadTpl);
                }
            })
            .catch(function (err) {
                overlay.classList.remove('visible');
                alert('Failed to start PDF generation: ' + err.message);
            });
    }

    function _poll(jobId, total, overlay, progressEl, cancelBtn, pollTpl, downloadTpl) {
        var pollUrl = pollTpl.replace('__JOBID__', jobId);
        var checkCount = 0;
        var maxChecks = 1200;

        function tick() {
            if (_cancelled) return;
            checkCount++;
            if (checkCount > maxChecks) {
                overlay.classList.remove('visible');
                alert('PDF generation timed out. Please try again.');
                return;
            }

            fetch(pollUrl, { method: 'GET', credentials: 'same-origin' })
                .then(function (r) { return r.json(); })
                .then(function (data) {
                    if (_cancelled) return;
                    if (data.status === 'processing') {
                        _updateProgress(data, total, progressEl);
                        _pollTimer = setTimeout(tick, 1500);
                    } else if (data.status === 'completed' || data.status === 'completed_with_errors') {
                        _handleComplete(data, overlay, progressEl, cancelBtn, downloadTpl, jobId);
                    } else if (data.status === 'error') {
                        overlay.classList.remove('visible');
                        alert('PDF generation failed: ' + (data.message || 'Unknown error'));
                    } else {
                        _pollTimer = setTimeout(tick, 2000);
                    }
                })
                .catch(function () {
                    if (!_cancelled) _pollTimer = setTimeout(tick, 3000);
                });
        }

        tick();
    }

    function cancel() {
        _cancelled = true;
        if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
        _cleanupWs();
        var overlay = document.getElementById('rcGenOverlay');
        var cancelBtn = document.getElementById('rcGenCancelBtn');
        if (overlay) overlay.classList.remove('visible');
        if (cancelBtn) cancelBtn.style.display = 'none';
        // Server-side cancel
        if (_jobId) {
            var cancelTpl = overlay ? overlay.getAttribute('data-cancel-url') : null;
            if (cancelTpl) {
                var cancelUrl = cancelTpl.replace('__JOBID__', _jobId);
                fetch(cancelUrl, { method: 'POST', credentials: 'same-origin' }).catch(function(){});
            }
            _jobId = null;
        }
    }

    return { start: start, cancel: cancel };
})();
