/* Report card viewport fit — PDF-viewer behaviour for phones.
 *
 * The report card is a fixed-width print document (7.9in ≈ 758px). On a
 * phone (~380px viewport) it used to require sideways scrolling. This
 * scales the card down with CSS `zoom` so that:
 *
 *   1. fit-to-width  — the card always fills the viewport width with zero
 *                      horizontal scrolling (all phones, all orientations);
 *   2. fit-to-page   — when showing the WHOLE card still keeps >=75% of
 *                      the width usable (large phones, e.g. 412x915), the
 *                      entire card is visible in one glance like a PDF
 *                      page, otherwise fit-to-width wins so text stays as
 *                      large as possible.
 *
 * Measurements never reset the zoom (no flicker): natural width is derived
 * from getBoundingClientRect() ÷ current zoom, and the page scale is the
 * zoom that makes the card's visual height equal the available height
 * (visual height scales linearly with zoom, so font/layout quirks cannot
 * make the card overshoot the bottom nav).
 *
 * Details are read by pinching to zoom (the page's meta viewport allows
 * zoom — do not add user-scalable=no). Desktop / landscape widths where
 * the card already fits are left at 100% scale untouched.
 *
 * Print/PDF output is unaffected: report-card.css and the pagePrintCSS
 * popup stylesheet both force `zoom: 1 !important` under print.
 */
(function () {
    'use strict';

    var SUPPORTED = (typeof CSS !== 'undefined' && CSS.supports && CSS.supports('zoom', '1.5'));
    var timer = null;

    /* ── Print arrangement on screen ────────────────────────────────────
       On phones the viewport media queries re-flow the card's INSIDES
       (top grid stacked, stats 3+2, 12pt type …) so it no longer matches
       the printed/PDF document. The popup print stylesheet (#pagePrintCSS)
       holds the canonical document arrangement — take ITS rules, scope
       every selector under BOTH scroller flavours (.rv-card-scroll for the
       Find Report / individual shell, .rc-card-scroll for the bulk
       #reportCardsContainer cards) and wrap them in
       @media (max-width: 1023px) so the card renders exactly like the
       printout while page chrome (nav, headers, backgrounds) is untouched.
       @page rules are dropped; rules whose selectors only match outside
       the card (body, html, .bottom-nav, #reportCardsContainer …) become
       harmless no-ops once scoped. */
    function injectPrintArrangement() {
        if (document.getElementById('rcPrintArrangement')) return;
        var src = document.getElementById('pagePrintCSS');
        if (!src) return;
        var css = src.textContent || '';
        var out = [];
        var i = 0, n = css.length;
        while (i < n) {
            while (i < n && /\s/.test(css.charAt(i))) i++;
            if (i >= n) break;
            if (css.substr(i, 2) === '/*') {
                var ce = css.indexOf('*/', i);
                i = ce < 0 ? n : ce + 2;
                continue;
            }
            var start = i;
            while (i < n && css.charAt(i) !== '{' && css.charAt(i) !== ';') i++;
            if (i >= n) break;
            if (css.charAt(i) === ';') { i++; continue; }        /* stray at-rule */
            var prelude = css.slice(start, i).trim();
            var depth = 0, j = i;
            for (; j < n; j++) {                                 /* match braces (@page nests) */
                if (css.charAt(j) === '{') depth++;
                else if (css.charAt(j) === '}') { depth--; if (!depth) break; }
            }
            if (j >= n) break;
            var body = css.slice(i + 1, j);
            i = j + 1;
            if (prelude.charAt(0) === '@') continue;             /* drop @page etc. */
            var scoped = prelude.split(',').map(function (s) {
                s = s.trim();
                return '.rv-card-scroll ' + s + ', .rc-card-scroll ' + s;
            }).join(', ');
            out.push(scoped + '{' + body + '}');
        }
        if (!out.length) return;
        /* Bulk cards carry a "swipe to see full report card" hint; once the
           card is fitted to the viewport there is nothing to swipe. */
        out.push('.rc-card-scroll::after { display: none !important; }');
        var st = document.createElement('style');
        st.id = 'rcPrintArrangement';
        st.textContent = '@media (max-width: 1023px) {\n' + out.join('\n') + '\n}';
        document.head.appendChild(st);
    }

    /* Bottom edge of FIXED chrome overlaying the page top. Only position:fixed
       counts — sticky bars move with the scroll and would make the fit
       scroll-position dependent (size flapping while scrolling). */
    function coverBottom() {
        var all = document.querySelectorAll('body *');
        var cover = 0;
        for (var i = 0; i < all.length; i++) {
            var cs = getComputedStyle(all[i]);
            if (cs.position !== 'fixed') continue;
            if (cs.visibility === 'hidden' || cs.display === 'none') continue;
            var cr = all[i].getBoundingClientRect();
            if (cr.width >= window.innerWidth * 0.5 && cr.height < 300 &&
                cr.top < 220 && cr.bottom > 0) {
                cover = Math.max(cover, cr.bottom);
            }
        }
        return cover;
    }

    function bottomNavRect() {
        var nav = document.querySelector('.bottom-nav');
        if (!nav) return null;
        var cs = getComputedStyle(nav);
        if (cs.display === 'none' || cs.visibility === 'hidden') return null;
        var nr = nav.getBoundingClientRect();
        if (nr.height <= 0 || nr.bottom <= 0 || nr.top >= window.innerHeight) return null;
        return nr;
    }

    function fitOne(sc) {
        var card = sc.querySelector('.report-card');
        if (!card) return;
        var cur = parseFloat(getComputedStyle(card).zoom) || 1;
        var availW = sc.clientWidth;
        if (availW < 50) return;                /* not laid out yet */

        /* Natural width at zoom 1: briefly clear zoom, read offsetWidth +
           margins, restore (same-frame reflow — no repaint/flicker). Needed
           because with width:100% + zoom Chromium resolves the percentage in
           the zoomed coordinate space, so rect/zoom over-reports the true
           unzoomed width (card then sticks at the old zoom forever).
           Margins count: bulk cards keep id-scoped 12px side margins that
           beat the scoped print reset, and they scale with the zoom too. */
        var natW, natH;
        if (cur !== 1) {
            card.style.zoom = '';
            var m = getComputedStyle(card);
            natW = card.offsetWidth + (parseFloat(m.marginLeft) || 0) +
                   (parseFloat(m.marginRight) || 0);
            natH = card.offsetHeight;
            card.style.zoom = cur;
        } else {
            var m1 = getComputedStyle(card);
            natW = card.offsetWidth + (parseFloat(m1.marginLeft) || 0) +
                   (parseFloat(m1.marginRight) || 0);
            natH = card.offsetHeight;
        }
        if (natW < 50) return;
        if (natW <= availW + 1) {                /* card fits at 100% (rotate/desktop) */
            if (cur !== 1) card.style.zoom = '';
            return;
        }

        var fitW = availW / natW;
        var fit = fitW;
        var availH = 0;

        /* Vertical room — STABLE, never derived from the card's current
           scroll position (that made the zoom flap between width-fit and
           whole-page-fit while scrolling, like large → normal → tiny).
           Fixed inputs only: fixed chrome at top, fixed bottom nav (or the
           fixed-height shell), so the size only ever changes on resize. */
        var vsc = sc.closest('.rv-scroll');
        if (vsc) {
            var vcs = getComputedStyle(vsc);
            if ((vcs.overflowY === 'auto' || vcs.overflowY === 'scroll') &&
                vsc.clientHeight < vsc.scrollHeight) {
                availH = vsc.clientHeight - (parseFloat(vcs.paddingTop) || 0) -
                         (parseFloat(vcs.paddingBottom) || 0);
            }
        }
        if (availH < 80) {
            var cover = coverBottom();
            var bottom = window.innerHeight;
            var nav = bottomNavRect();
            if (nav) bottom = nav.top;
            availH = bottom - cover;
        }
        if (availH > 80) {
            /* Zoom at which the whole card fits between chrome and nav: */
            var zPage = availH / natH;
            if (zPage > fitW) zPage = fitW;          /* width-fit already short enough */
            /* Whole-page only when it still keeps >=75% of the width
               usable (zPage >= 0.75 * fitW), else stay at width-fit so
               text stays as large as possible instead of a stamp. */
            if (zPage >= fitW * 0.75) fit = zPage;
        }

        if (Math.abs(fit - cur) > 0.0005) card.style.zoom = fit;

        /* Layout rounding makes the zoom↔height relation slightly non-linear,
           so verify a page fit actually lands within the room and nudge it
           (bounded loop; stops within ~1.5px). */
        if (availH > 80 && fit < fitW - 0.002) {
            var z = fit;
            for (var i = 0; i < 4; i++) {
                var ghNow = sc.getBoundingClientRect().height;
                if (ghNow <= availH + 1.5) break;
                z = z * (availH / ghNow);
                if (z >= fitW - 0.0005) { z = fitW; }
                card.style.zoom = z;
                if (z === fitW) break;
            }
        }
    }

    function fitAll() {
        if (!SUPPORTED || !document.querySelectorAll) return;
        var scrollers = document.querySelectorAll('.rv-card-scroll, .rc-card-scroll');
        for (var i = 0; i < scrollers.length; i++) fitOne(scrollers[i]);
    }

    function schedule(delay) {
        if (timer) clearTimeout(timer);
        timer = setTimeout(function () { timer = null; fitAll(); }, delay || 150);
    }

    function boot() {
        injectPrintArrangement();
        fitAll();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot);
    } else {
        boot();
    }
    document.addEventListener('htmx:afterSwap', function () {
        injectPrintArrangement();                   /* idempotent */
        fitAll();                                   /* immediate, … */
        schedule(500);                              /* … and once layout settles */
    });
    /* Deliberately NO scroll/scrollend refits: the fit uses only stable
       inputs (fixed chrome, nav, scroller width), so scrolling never
       changes the size — refitting on scroll would only cause visible
       size flapping and layout jank. */
    window.addEventListener('resize', schedule);
    window.addEventListener('orientationchange', schedule);
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(function () { schedule(); });
    }
})();
