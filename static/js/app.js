/* ============================================================================
   EDUNEXUS — app shell behaviour
   Loaded synchronously at the end of <body>, in place of the inline block that
   used to live in students/base.html.  Nothing here is wrapped in an IIFE:
   page templates call showLoader()/hideLoader()/showToast() directly, and
   headteacher_comments_modal / profile check for window.showToast.
   ========================================================================== */

        // ── Mobile Menu Toggle ──────────────────────────────────
        const hamburgerBtn = document.getElementById('hamburgerBtn');
        const sidebar = document.getElementById('sidebar');
        const sidebarOverlay = document.getElementById('sidebarOverlay');

        function openMenu() {
            if (sidebar) sidebar.classList.add('open', 'active');
            if (sidebarOverlay) sidebarOverlay.classList.add('active', 'visible');
            if (hamburgerBtn) hamburgerBtn.classList.add('active');
        }
        function closeMenu() {
            if (sidebar) sidebar.classList.remove('open', 'active');
            if (sidebarOverlay) sidebarOverlay.classList.remove('active', 'visible');
            if (hamburgerBtn) hamburgerBtn.classList.remove('active');
        }

        if (hamburgerBtn) hamburgerBtn.addEventListener('click', () => {
            sidebar.classList.contains('active') ? closeMenu() : openMenu();
        });
        if (sidebarOverlay) sidebarOverlay.addEventListener('click', closeMenu);

        if (sidebar) sidebar.querySelectorAll('.ps-nav-link').forEach(link => {
            link.addEventListener('click', closeMenu);
        });

        window.addEventListener('resize', () => {
            if (window.innerWidth > 1023) closeMenu();
        });

        // ── Mobile Menu Sheet ────────────────────────────────────
        const mobileMenuBtn = document.getElementById('mobileMenuBtn');
        const mobileMenuSheet = document.getElementById('mobileMenuSheet');
        const mobileMenuBackdrop = document.getElementById('mobileMenuBackdrop');
        const mobileMenuClose = document.getElementById('mobileMenuClose');

        function openMobileMenu() {
            if (!mobileMenuSheet) return;
            mobileMenuSheet.classList.add('open');
            mobileMenuSheet.setAttribute('aria-hidden', 'false');
            if (mobileMenuBtn) mobileMenuBtn.setAttribute('aria-expanded', 'true');
        }

        function closeMobileMenu() {
            if (!mobileMenuSheet) return;
            mobileMenuSheet.classList.remove('open');
            mobileMenuSheet.setAttribute('aria-hidden', 'true');
            if (mobileMenuBtn) mobileMenuBtn.setAttribute('aria-expanded', 'false');
        }

        if (mobileMenuBtn) {
            mobileMenuBtn.addEventListener('click', function() {
                if (!mobileMenuSheet) return;
                if (mobileMenuSheet.classList.contains('open')) {
                    closeMobileMenu();
                } else {
                    openMobileMenu();
                }
            });
        }

        if (mobileMenuBackdrop) mobileMenuBackdrop.addEventListener('click', closeMobileMenu);
        if (mobileMenuClose) mobileMenuClose.addEventListener('click', closeMobileMenu);

        if (mobileMenuSheet) {
            mobileMenuSheet.querySelectorAll('.mobile-menu-link[href]').forEach(link => {
                link.addEventListener('click', closeMobileMenu);
            });
        }

        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') closeMobileMenu();
            // Ctrl+K / Cmd+K — focus search
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
                e.preventDefault();
                var searchInput = document.querySelector('.header-search-input, .mobile-search-input');
                if (searchInput) searchInput.focus();
            }
        });

        // ── Global Loading Overlay ───────────────────────────────
        // showLoader(passive): the initial page-load overlay is passive so
        // it never blocks scrolling/touch while the page is still arriving;
        // navigation/submit loaders stay blocking (double-tap protection).
        const globalLoader = document.getElementById('globalLoader');
        function showLoader(passive) {
            if (globalLoader) {
                globalLoader.classList.add('active');
                globalLoader.classList.toggle('passive', !!passive);
            }
        }
        function hideLoader() { if (globalLoader) globalLoader.classList.remove('active', 'passive'); }

        // ── Toast Notifications ─────────────────────────────────
        function showToast(message, type) {
            type = type || 'info';
            var existing = document.querySelector('.en-toast');
            if (existing) existing.remove();
            var icons = {
                success: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>',
                error: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
                warning: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
                info: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
            };
            var toast = document.createElement('div');
            toast.className = 'en-toast en-toast-' + type;
            toast.innerHTML = '<span class="toast-icon">' + (icons[type] || icons.info) + '</span><span>' + message + '</span>';
            document.body.appendChild(toast);
            setTimeout(function(){ toast.classList.add('visible'); }, 10);
            setTimeout(function(){ toast.classList.remove('visible'); setTimeout(function(){ toast.remove(); }, 300); }, 4000);
        }

        // Override alert() with toast for non-critical messages
        window._originalAlert = window.alert;
        window.alert = function(msg) { showToast(msg, 'error'); };

        // Surface failed HTMX requests (4xx/5xx are not swapped) as toasts.
        // Without this, a rejected hx-get looks like a dead tap.
        document.addEventListener('htmx:responseError', function(e) {
            var xhr = e.detail && e.detail.xhr;
            var status = xhr ? xhr.status : 0;
            var path = (xhr && xhr.responseURL) || '';
            if (path.indexOf('/save-mark') !== -1 || path.indexOf('/return') !== -1) return;
            showToast('Request failed' + (status ? ' (HTTP ' + status + ')' : '') + '. Refresh and try again.', 'error');
        });
        document.addEventListener('htmx:sendError', function() {
            showToast('Network error — check your connection.', 'error');
            hideLoader();
        });

        document.addEventListener('htmx:afterRequest', function() {
            hideLoader();
        });

        showLoader(true);
        window.addEventListener('load', hideLoader);
        setTimeout(hideLoader, 3000);

        document.querySelectorAll('.nav-link-card, .btn, a[href]').forEach(el => {
            if (el.hasAttribute('hx-get') || el.hasAttribute('hx-post') || el.hasAttribute('hx-trigger')) return;
            el.addEventListener('click', function() {
                const href = this.getAttribute('href');
                if (href && href !== '#' && !href.startsWith('javascript:') && !href.startsWith('mailto:')) {
                    showLoader();
                    setTimeout(hideLoader, 3000);
                }
            });
        });

        document.querySelectorAll('form').forEach(form => {
            form.addEventListener('submit', function() {
                showLoader();
                setTimeout(hideLoader, 12000);
            });
        });

        window.showLoader = showLoader;
        window.hideLoader = hideLoader;

        // ── Chart.js auto-init for HTMX-swapped canvases ─────────
        function initAnalyticsCharts() {
            // ── Subject Performance chart ──
            var canvas = document.getElementById('performanceLineChart');
            if (canvas && !canvas._chartInit && typeof Chart !== 'undefined') {
                canvas._chartInit = true;
                var ctx = canvas.getContext('2d');
                var g = window.__chartData || {};
                var labels = g.labels || (canvas.dataset.labels ? JSON.parse(canvas.dataset.labels) : []);
                var studentData = g.studentData || (canvas.dataset.studentData ? JSON.parse(canvas.dataset.studentData) : []);
                var formData = g.formData || (canvas.dataset.formData ? JSON.parse(canvas.dataset.formData) : []);
                var targetData = g.targetData || (canvas.dataset.targetData ? JSON.parse(canvas.dataset.targetData) : []);
                var studentName = g.studentName || canvas.dataset.studentName || 'Student';
                var classLabel = g.classLabel || canvas.dataset.classLabel || 'Class';
                if (labels.length && !(labels.length === 1 && labels[0] === 'No Data')) {
                    var gradStudent = ctx.createLinearGradient(0, 0, 0, 360);
                    gradStudent.addColorStop(0, 'rgba(34,197,94,0.12)');
                    gradStudent.addColorStop(1, 'rgba(34,197,94,0.01)');
                    new Chart(ctx, {
                        type: 'line',
                        data: {
                            labels: labels,
                            datasets: [
                                {
                                    label: 'Form Average',
                                    data: formData,
                                    borderColor: '#94a3b8',
                                    backgroundColor: 'rgba(226,232,240,0.4)',
                                    borderWidth: 2,
                                    pointBackgroundColor: '#94a3b8',
                                    pointBorderColor: '#ffffff',
                                    pointBorderWidth: 2,
                                    pointRadius: 4,
                                    pointHoverRadius: 6,
                                    fill: true,
                                    tension: 0.3
                                },
                                {
                                    label: studentName,
                                    data: studentData,
                                    borderColor: '#22c55e',
                                    backgroundColor: gradStudent,
                                    borderWidth: 3,
                                    pointBackgroundColor: '#ffffff',
                                    pointBorderColor: '#22c55e',
                                    pointBorderWidth: 2,
                                    pointRadius: 5,
                                    pointHoverRadius: 7,
                                    fill: true,
                                    tension: 0.3
                                }
                            ]
                        },
                        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: true, position: 'top', align: 'end', labels: { usePointStyle: true, pointStyle: 'circle', padding: 16, font: { family: 'Inter', size: 12, weight: '500' }, color: '#64748b' } }, tooltip: { backgroundColor: '#0f172a', titleFont: { family: 'Inter', size: 12 }, bodyFont: { family: 'Inter', size: 13, weight: '600' }, padding: 12, cornerRadius: 8 } }, scales: { x: { grid: { display: false }, ticks: { font: { family: 'Inter', size: 11 }, color: '#94a3b8' } }, y: { beginAtZero: true, max: 100, grid: { color: '#f1f5f9' }, ticks: { font: { family: 'Inter', size: 11 }, color: '#94a3b8', stepSize: 10 } } } }
                    });
                }
            }
            // ── Performance Over Time chart ──
            var canvas2 = document.getElementById('performanceTimelineChart');
            if (canvas2 && !canvas2._chartInit && typeof Chart !== 'undefined') {
                canvas2._chartInit = true;
                var ctx2 = canvas2.getContext('2d');
                var gt = window.__timelineData || {};
                var tlLabels = gt.labels || (canvas2.dataset.tlLabels ? JSON.parse(canvas2.dataset.tlLabels) : []);
                var tlStudent = gt.student || (canvas2.dataset.tlStudent ? JSON.parse(canvas2.dataset.tlStudent) : []);
                if (tlLabels.length) {
                    var gradTimeline = ctx2.createLinearGradient(0, 0, 0, 280);
                    gradTimeline.addColorStop(0, 'rgba(34,197,94,0.10)');
                    gradTimeline.addColorStop(1, 'rgba(34,197,94,0.01)');
                    new Chart(ctx2, {
                        type: 'line',
                        data: {
                            labels: tlLabels,
                            datasets: [
                                {
                                    label: 'Exam Mean',
                                    data: tlStudent,
                                    borderColor: '#22c55e',
                                    backgroundColor: gradTimeline,
                                    borderWidth: 2,
                                    pointBackgroundColor: '#ffffff',
                                    pointBorderColor: '#22c55e',
                                    pointBorderWidth: 2,
                                    pointRadius: 4,
                                    pointHoverRadius: 6,
                                    fill: true,
                                    tension: 0.3
                                }
                            ]
                        },
                        options: {
                            responsive: true,
                            maintainAspectRatio: false,
                            plugins: {
                                legend: { display: false },
                                tooltip: {
                                    backgroundColor: '#0f172a',
                                    titleFont: { family: 'Inter', size: 11, weight: '600' },
                                    bodyFont: { family: 'Inter', size: 12, weight: '600' },
                                    padding: 10,
                                    cornerRadius: 8,
                                    displayColors: false,
                                    callbacks: {
                                        title: function(items) { return items[0].label; },
                                        label: function(item) { return 'Exam Mean: ' + item.formattedValue; }
                                    }
                                }
                            },
                            scales: {
                                x: {
                                    grid: { display: false },
                                    ticks: { font: { family: 'Inter', size: 10 }, color: '#94a3b8', maxRotation: 50, minRotation: 50 }
                                },
                                y: {
                                    beginAtZero: true,
                                    max: 100,
                                    grid: { color: '#f1f5f9' },
                                    ticks: { font: { family: 'Inter', size: 11 }, color: '#94a3b8', stepSize: 50 }
                                }
                            }
                        }
                    });
                }
            }
        }
        document.addEventListener('htmx:afterSettle', initAnalyticsCharts);
        document.addEventListener('htmx:afterSwap', initAnalyticsCharts);
        document.addEventListener('DOMContentLoaded', initAnalyticsCharts);
        setTimeout(initAnalyticsCharts, 500);

        // ── Header User Dropdown ─────────────────────────────────
        const headerUser = document.getElementById('headerUserMenu');
        if (headerUser) {
            headerUser.addEventListener('click', function(e) {
                e.stopPropagation();
                this.classList.toggle('open');
            });
            document.addEventListener('click', () => headerUser.classList.remove('open'));
        }

        // ── Mobile Header Dropdown ───────────────────────────────
        const mgUser = document.getElementById('mgUserMenu');
        const mgDrop = document.getElementById('mgDropdown');
        if (mgUser && mgDrop) {
            mgUser.addEventListener('click', function(e) {
                e.stopPropagation();
                mgDrop.classList.toggle('open');
            });
            document.addEventListener('click', function() {
                mgDrop.classList.remove('open');
            });
        }
