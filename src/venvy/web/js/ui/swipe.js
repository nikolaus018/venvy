/**
 * Touch swipe gestures for opening and closing the sidebar on mobile.
 */

import { closeSidebar, openSidebar } from "./sidebar.js";

export let touchStartX = 0, touchCurrentX = 0, isSwipingSidebar = false, hasMoved = false;

const sidebar = document.getElementById('main-sidebar');

const overlay = document.getElementById('sidebar-overlay');

export const SWIPE_THRESHOLD = 30, SIDEBAR_WIDTH = 280;

document.addEventListener('touchstart', (e) => {
    touchStartX = e.touches[0].clientX; hasMoved = false;
    const isClosed = sidebar.classList.contains('-translate-x-full');
    if (isClosed && touchStartX < SWIPE_THRESHOLD) isSwipingSidebar = true;
    if (!isClosed && e.target.closest('#main-sidebar')) { isSwipingSidebar = true; sidebar.style.transition = 'none'; overlay.style.transition = 'none'; }
}, { passive: true });

document.addEventListener('touchmove', (e) => {
    if (!isSwipingSidebar) return;
    touchCurrentX = e.touches[0].clientX;
    if (Math.abs(touchCurrentX - touchStartX) < 5) return;
    if (!hasMoved) {
        hasMoved = true; sidebar.style.transition = 'none';
        if (sidebar.classList.contains('-translate-x-full')) { overlay.classList.remove('hidden'); overlay.style.transition = 'none'; overlay.style.display = 'block'; }
        else overlay.style.transition = 'none';
    }
    let translateX = 0;
    const isClosedInitially = sidebar.classList.contains('-translate-x-full');
    if (isClosedInitially) translateX = Math.min(0, touchCurrentX - SIDEBAR_WIDTH);
    else translateX = Math.min(0, touchCurrentX - touchStartX);
    sidebar.style.transform = `translateX(${translateX}px)`;
    const progress = 1 - (Math.abs(translateX) / SIDEBAR_WIDTH);
    overlay.style.opacity = Math.max(0, Math.min(0.6, progress * 0.6));
}, { passive: true });

document.addEventListener('touchend', (_e) => {
    if (!isSwipingSidebar) return;
    isSwipingSidebar = false;
    if (!hasMoved) return;
    sidebar.style.transition = 'transform 0.28s cubic-bezier(0.4, 0, 0.2, 1)';
    overlay.style.transition = 'opacity 0.3s ease';
    const transformMatch = sidebar.style.transform.match(/translateX\(([-\d.]+)px\)/);
    const currentTranslate = transformMatch ? parseFloat(transformMatch[1]) : 0;
    sidebar.style.transform = ''; overlay.style.opacity = ''; overlay.style.display = '';
    if (currentTranslate > -(SIDEBAR_WIDTH / 2)) openSidebar(); else closeSidebar();
});
