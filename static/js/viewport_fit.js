/*
 * viewport_fit.js — one fix for "everything looks tiny on a phone".
 *
 * Cause: when a phone's browser forces a desktop layout (Chrome/Safari
 * "Desktop site", or any agent ignoring the viewport meta), the layout
 * viewport becomes ~700-980+ CSS px wide while the physical screen is
 * ~360-430. The browser then shrink-to-fits the whole page down to the
 * screen (scale = screen.width / innerWidth) -> the page renders full
 * but EVERYTHING (text, icons, paddings) is a fraction of normal size.
 *
 * Countermeasure: html { zoom: innerWidth / screen.width } is the exact
 * inverse of that downscale (verified in Chromium: zoom scales content
 * WITHOUT horizontal overflow because children lay out in a compressed
 * coordinate space). Net visual scale becomes 1.0 -> correct size.
 *
 * Also toggles html.force-mobile so the mobile chrome/layout is used
 * while the wide layout is being compensated (markPhone in base.html
 * handles normal phones; this covers wide-layout states markPhone
 * misses, e.g. landscape screen.width > 500 with a desktop UA).
 *
 * The zoom is CLEARED whenever the viewport is normal again (user
 * un-checks "Desktop site", rotates, etc.) so it can never get stuck
 * and "explode" the text.
 *
 * Guards against false positives on real desktops:
 *   - pointer must be coarse (touch-primary device)
 *   - innerWidth >= 700 (no phone renders wider than 700 normally)
 *   - innerWidth >= screen.width * 1.03 (layout wider than the screen
 *     only happens when shrink-to-fit is active)
 */
(function () {
    function fitViewport() {
        try {
            var el = document.documentElement;
            var vw = window.innerWidth || 0;
            var sw = (window.screen && window.screen.width) || 0;
            var coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
            var wideLayoutOnPhone = coarse && sw > 0 && vw >= 700 && vw >= sw * 1.03;
            if (wideLayoutOnPhone) {
                var ratio = Math.round((vw / sw) * 1000) / 1000;
                el.style.zoom = String(ratio);
                el.classList.add('force-mobile');
            } else if (el.style.zoom) {
                el.style.zoom = '';
            }
        } catch (e) {}
    }
    fitViewport();
    window.addEventListener('load', fitViewport);
    window.addEventListener('pageshow', fitViewport);
    window.addEventListener('resize', fitViewport, { passive: true });
    window.addEventListener('orientationchange', fitViewport, { passive: true });
})();
