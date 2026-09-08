/**
 * Premium bulk PDF generation with progress ring, ETA, and celebration.
 *
 * Usage from a template:
 *   <div id="rcGenOverlay"
 *        data-start-url="..."
 *        data-poll-url="..."
 *        data-download-url="..."
 *        data-cancel-url="..."
 *        data-ws-url="...">
 *     ...
 *   </div>
 *
 * Then call: BulkPDF.start(selectedIds, { grade, stream, examId, ... })
 */
var BulkPDF = (function () {
    var _pollTimer = null;
    var _cancelled = false;
    var _ws = null;
    var _jobId = null;
    var _startTime = 0;
    var _lastCompiled = 0;
    var _lastTime = 0;

    /* ── Helpers ─────────────────────────────────────────────────────── */

    function _buildStartUrl(base, params, ids) {
        var sep = base.indexOf('?') === -1 ? '?' : '&';
        var parts = [];
        for (var k in params) {
            if (params[k]) parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(params[k]));
        }
        if (ids && ids.length) parts.push('ids=' + ids.join(','));
        return base + sep + parts.join('&');
    }

    function _formatTime(seconds) {
        if (seconds < 60) return Math.ceil(seconds) + 's';
        var m = Math.floor(seconds / 60);
        var s = Math.ceil(seconds % 60);
        return m + 'm ' + s + 's';
    }

    function _formatSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(0) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    /* ── Progress Ring ───────────────────────────────────────────────── */

    function _createRing(container) {
        var R = 54, C = 2 * Math.PI * R;
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('width', '136');
        svg.setAttribute('height', '136');
        svg.setAttribute('viewBox', '0 0 136 136');
        svg.style.transform = 'rotate(-90deg)';

        var track = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        track.setAttribute('cx', '68');
        track.setAttribute('cy', '68');
        track.setAttribute('r', String(R));
        track.setAttribute('fill', 'none');
        track.setAttribute('stroke', '#E5E7EB');
        track.setAttribute('stroke-width', '8');

        var fill = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        fill.setAttribute('cx', '68');
        fill.setAttribute('cy', '68');
        fill.setAttribute('r', String(R));
        fill.setAttribute('fill', 'none');
        fill.setAttribute('stroke', '#3B82F6');
        fill.setAttribute('stroke-width', '8');
        fill.setAttribute('stroke-linecap', 'round');
        fill.setAttribute('stroke-dasharray', String(C));
        fill.setAttribute('stroke-dashoffset', String(C));
        fill.style.transition = 'stroke-dashoffset 0.6s cubic-bezier(0.4, 0, 0.2, 1), stroke 0.3s ease';

        svg.appendChild(track);
        svg.appendChild(fill);
        container.appendChild(svg);

        return {
            setProgress: function (pct) {
                var offset = C * (1 - pct / 100);
                fill.setAttribute('stroke-dashoffset', String(offset));
                if (pct >= 100) fill.setAttribute('stroke', '#22C55E');
                else if (pct >= 60) fill.setAttribute('stroke', '#3B82F6');
                else fill.setAttribute('stroke', '#3B82F6');
            },
            celebrate: function () {
                fill.setAttribute('stroke', '#22C55E');
                fill.setAttribute('stroke-dashoffset', '0');
            }
        };
    }

    /* ── Confetti ────────────────────────────────────────────────────── */

    function _spawnConfetti(card) {
        var colors = ['#3B82F6', '#22C55E', '#F59E0B', '#EF4444', '#8B5CF6', '#EC4899'];
        for (var i = 0; i < 40; i++) {
            (function (idx) {
                var el = document.createElement('div');
                var size = 6 + Math.random() * 6;
                el.style.cssText = 'position:absolute;width:' + size + 'px;height:' + size + 'px;'
                    + 'background:' + colors[idx % colors.length] + ';border-radius:'
                    + (Math.random() > 0.5 ? '50%' : '2px') + ';pointer-events:none;z-index:1000;'
                    + 'top:-10px;left:' + (20 + Math.random() * 60) + '%;opacity:0;'
                    + 'animation:bpdf-confetti ' + (1.5 + Math.random() * 1.5) + 's ease-out '
                    + (Math.random() * 0.5) + 's forwards;';
                card.appendChild(el);
                setTimeout(function () { if (el.parentNode) el.remove(); }, 4000);
            })(i);
        }
    }

    /* ── Overlay DOM Builder ─────────────────────────────────────────── */

    function _buildOverlay(overlay, total, estMB) {
        var card = overlay.querySelector('.rc-gen-card');
        if (!card) return null;

        // Save grade/exam text BEFORE clearing innerHTML (they live inside the card)
        var gradeEl = document.getElementById('rcGenGrade');
        var examEl = document.getElementById('rcGenExam');
        var gradeText = gradeEl ? gradeEl.textContent : '';
        var examText = examEl ? examEl.textContent : '';

        card.innerHTML = '';

        // Header
        var header = document.createElement('div');
        header.className = 'rc-gen-card-header';
        header.innerHTML = '<h2>Generating Report Cards</h2>';
        card.appendChild(header);

        // Summary
        if (gradeText || examText) {
            var summary = document.createElement('div');
            summary.className = 'rc-gen-summary';
            if (gradeText) {
                var g = document.createElement('div');
                g.className = 'rc-gen-summary-grade';
                g.textContent = gradeText;
                summary.appendChild(g);
            }
            if (examText) {
                var e = document.createElement('div');
                e.className = 'rc-gen-summary-exam';
                e.textContent = examText;
                summary.appendChild(e);
            }
            card.appendChild(summary);
        }

        // Progress ring + center text
        var ringWrap = document.createElement('div');
        ringWrap.style.cssText = 'display:flex;justify-content:center;margin:20px 0 12px;position:relative;';
        var ring = _createRing(ringWrap);

        var centerText = document.createElement('div');
        centerText.style.cssText = 'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);'
            + 'text-align:center;pointer-events:none;';
        centerText.innerHTML = '<div class="bpdf-pct" style="font-size:28px;font-weight:800;'
            + 'color:#1E293B;line-height:1;">0%</div>'
            + '<div class="bpdf-eta" style="font-size:11px;color:#94A3B8;margin-top:2px;"></div>';
        ringWrap.appendChild(centerText);
        card.appendChild(ringWrap);

        // Stage indicator
        var stageEl = document.createElement('div');
        stageEl.className = 'bpdf-stage';
        stageEl.style.cssText = 'text-align:center;font-size:12px;font-weight:700;'
            + 'color:#64748B;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:8px;';
        stageEl.textContent = 'Preparing...';
        card.appendChild(stageEl);

        // Detail text
        var detailEl = document.createElement('div');
        detailEl.className = 'bpdf-detail';
        detailEl.style.cssText = 'text-align:center;font-size:13px;color:#475569;'
            + 'margin-bottom:4px;min-height:20px;';
        detailEl.textContent = 'Setting up ' + total + ' report cards...';
        card.appendChild(detailEl);

        // Progress bar
        var barWrap = document.createElement('div');
        barWrap.style.cssText = 'height:6px;background:#E5E7EB;border-radius:3px;'
            + 'margin:0 24px 8px;overflow:hidden;';
        var barFill = document.createElement('div');
        barFill.className = 'bpdf-bar';
        barFill.style.cssText = 'height:100%;width:0%;background:linear-gradient(90deg,#3B82F6,#60A5FA);'
            + 'border-radius:3px;transition:width 0.5s cubic-bezier(0.4,0,0.2,1);';
        barWrap.appendChild(barFill);
        card.appendChild(barWrap);

        // Stats row
        var statsEl = document.createElement('div');
        statsEl.className = 'bpdf-stats';
        statsEl.style.cssText = 'display:flex;justify-content:space-between;padding:0 24px;'
            + 'font-size:11px;color:#94A3B8;';
        statsEl.innerHTML = '<span class="bpdf-count">0 / ' + total + ' compiled</span>'
            + '<span class="bpdf-est">~' + estMB + ' MB estimated</span>';
        card.appendChild(statsEl);

        // Cancel button
        var cancelBtn = document.createElement('button');
        cancelBtn.type = 'button';
        cancelBtn.id = 'rcGenCancelBtn';
        cancelBtn.textContent = 'Cancel';
        cancelBtn.style.cssText = 'display:block;margin:14px auto 0;padding:8px 28px;'
            + 'border:2px solid #dc2626;background:#fff;color:#dc2626;border-radius:8px;'
            + 'font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;'
            + 'transition:all 0.2s;';
        cancelBtn.onmouseenter = function () { cancelBtn.style.background = '#FEF2F2'; };
        cancelBtn.onmouseleave = function () { cancelBtn.style.background = '#fff'; };
        cancelBtn.onclick = function () { BulkPDF.cancel(); };
        card.appendChild(cancelBtn);

        // Inject confetti keyframes if not already present
        if (!document.getElementById('bpdf-keyframes')) {
            var style = document.createElement('style');
            style.id = 'bpdf-keyframes';
            style.textContent = '@keyframes bpdf-confetti{'
                + '0%{transform:translateY(0) rotate(0deg);opacity:1;}'
                + '100%{transform:translateY(300px) rotate(720deg);opacity:0;}}';
            document.head.appendChild(style);
        }

        return {
            ring: ring,
            pct: centerText.querySelector('.bpdf-pct'),
            eta: centerText.querySelector('.bpdf-eta'),
            stage: stageEl,
            detail: detailEl,
            bar: barFill,
            count: statsEl.querySelector('.bpdf-count'),
            cancelBtn: cancelBtn
        };
    }

    /* ── Progress Update ─────────────────────────────────────────────── */

    function _updateProgress(data, total, ui) {
        if (data.status !== 'processing' || !ui) return;

        var compiled = data.compiled || 0;
        var failed = data.failed || 0;
        var pct = total > 0 ? Math.round((compiled / total) * 100) : 0;

        // Rate & ETA
        var now = Date.now();
        if (compiled > _lastCompiled) {
            var dt = (now - _lastTime) / 1000;
            var dc = compiled - _lastCompiled;
            if (dt > 0) {
                var rate = dc / dt;
                var remaining = total - compiled;
                var eta = remaining / rate;
                ui.eta.textContent = '≈ ' + _formatTime(eta) + ' remaining';
            }
            _lastCompiled = compiled;
            _lastTime = now;
        }

        // Ring + percentage
        ui.ring.setProgress(pct);
        ui.pct.textContent = pct + '%';

        // Bar
        ui.bar.style.width = pct + '%';

        // Count
        var countText = compiled + ' / ' + total + ' compiled';
        if (failed > 0) countText += '  ·  ' + failed + ' failed';
        ui.count.textContent = countText;

        // Stage text
        if (pct < 10) {
            ui.stage.textContent = 'Preparing';
            ui.detail.textContent = 'Loading student data...';
        } else if (pct < 90) {
            ui.stage.textContent = 'Compiling';
            ui.detail.textContent = 'Rendering ' + compiled + ' of ' + total + ' report cards...';
        } else {
            ui.stage.textContent = 'Stitching';
            ui.detail.textContent = 'Assembling final PDF...';
        }
    }

    /* ── Completion ──────────────────────────────────────────────────── */

    function _handleComplete(data, overlay, ui, downloadTpl, jobId) {
        if (!ui) {
            overlay.classList.remove('visible');
            return;
        }

        var compiled = data.compiled || 0;
        var failed = data.failed || 0;

        // Final state
        ui.ring.celebrate();
        ui.pct.textContent = '100%';
        ui.eta.textContent = '';
        ui.bar.style.width = '100%';
        ui.bar.style.background = 'linear-gradient(90deg, #22C55E, #4ADE80)';
        ui.stage.textContent = 'Complete';
        ui.stage.style.color = '#22C55E';
        ui.detail.textContent = compiled + ' report cards ready'
            + (failed > 0 ? '  ·  ' + failed + ' failed' : '');
        ui.count.textContent = _formatSize(data.size || 0) + '  ·  Done in ' +
            _formatTime((Date.now() - _startTime) / 1000);
        ui.cancelBtn.style.display = 'none';

        // Confetti — append to overlay (not card) to avoid overflow:hidden clipping
        if (overlay) _spawnConfetti(overlay);

        // Auto-download
        var downloadUrl = downloadTpl.replace('__JOBID__', jobId);
        setTimeout(function () {
            window.location.href = downloadUrl;
        }, 800);

        // Dismiss after delay
        setTimeout(function () {
            overlay.style.transition = 'opacity 0.4s ease';
            overlay.style.opacity = '0';
            setTimeout(function () {
                overlay.classList.remove('visible');
                overlay.style.opacity = '';
                overlay.style.transition = '';
            }, 400);
        }, 3000);

        _cleanupWs();
    }

    /* ── WebSocket ───────────────────────────────────────────────────── */

    function _cleanupWs() {
        if (_ws) { try { _ws.close(); } catch (e) {} _ws = null; }
    }

    function _connectWs(jobId, total, overlay, ui, downloadTpl) {
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
                        _updateProgress(data, total, ui);
                    } else if (data.status === 'completed' || data.status === 'completed_with_errors') {
                        _handleComplete(data, overlay, ui, downloadTpl, jobId);
                    } else if (data.status === 'error') {
                        overlay.classList.remove('visible');
                        alert('PDF generation failed: ' + (data.message || 'Unknown error'));
                        _cleanupWs();
                    }
                } catch (e) {}
            };
            _ws.onclose = function () {
                if (!_cancelled) _poll(jobId, total, overlay, ui, downloadTpl);
            };
            _ws.onerror = function () {
                _cleanupWs();
                if (!_cancelled) _poll(jobId, total, overlay, ui, downloadTpl);
            };
            return true;
        } catch (e) {
            return false;
        }
    }

    /* ── Polling Fallback ────────────────────────────────────────────── */

    function _poll(jobId, total, overlay, ui, downloadTpl) {
        var overlayEl = document.getElementById('rcGenOverlay');
        var pollTpl = overlayEl ? overlayEl.getAttribute('data-poll-url') : '';
        if (!pollTpl) return;
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
                        _updateProgress(data, total, ui);
                        _pollTimer = setTimeout(tick, 1000);
                    } else if (data.status === 'completed' || data.status === 'completed_with_errors') {
                        _handleComplete(data, overlay, ui, downloadTpl, jobId);
                    } else if (data.status === 'error') {
                        overlay.classList.remove('visible');
                        alert('PDF generation failed: ' + (data.message || 'Unknown error'));
                    } else {
                        _pollTimer = setTimeout(tick, 1500);
                    }
                })
                .catch(function () {
                    if (!_cancelled) _pollTimer = setTimeout(tick, 2000);
                });
        }
        tick();
    }

    /* ── Public API ──────────────────────────────────────────────────── */

    function start(selectedIds, params) {
        if (!selectedIds || !selectedIds.length) {
            alert('Please select at least one learner first.');
            return;
        }

        var estMB = Math.round(selectedIds.length * 0.1);

        var overlay = document.getElementById('rcGenOverlay');
        if (!overlay) return;

        _cancelled = false;
        _startTime = Date.now();
        _lastCompiled = 0;
        _lastTime = _startTime;

        overlay.classList.add('visible');
        overlay.style.opacity = '1';

        var ui = _buildOverlay(overlay, selectedIds.length, estMB);
        if (!ui) return;

        var startUrl = overlay.getAttribute('data-start-url');
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
                ui.stage.textContent = 'Compiling';
                ui.detail.textContent = 'Compiling ' + total + ' report cards...';

                if (!_connectWs(jobId, total, overlay, ui, downloadTpl)) {
                    _poll(jobId, total, overlay, ui, downloadTpl);
                }
            })
            .catch(function (err) {
                overlay.classList.remove('visible');
                alert('Failed to start PDF generation: ' + err.message);
            });
    }

    function cancel() {
        _cancelled = true;
        if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
        _cleanupWs();
        var overlay = document.getElementById('rcGenOverlay');
        if (overlay) {
            overlay.style.transition = 'opacity 0.3s ease';
            overlay.style.opacity = '0';
            setTimeout(function () {
                overlay.classList.remove('visible');
                overlay.style.opacity = '';
                overlay.style.transition = '';
            }, 300);
        }
        if (_jobId) {
            var cancelTpl = overlay ? overlay.getAttribute('data-cancel-url') : null;
            if (cancelTpl) {
                var cancelUrl = cancelTpl.replace('__JOBID__', _jobId);
                fetch(cancelUrl, { method: 'POST', credentials: 'same-origin' }).catch(function () { });
            }
            _jobId = null;
        }
    }

    return { start: start, cancel: cancel };
})();
