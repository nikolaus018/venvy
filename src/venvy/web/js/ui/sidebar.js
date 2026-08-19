/**
 * The navigation sidebar.
 */

export function openSidebar() {
    const sidebar = document.getElementById('main-sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebar.classList.remove('-translate-x-full');
    sidebar.style.transform = ''; sidebar.style.transition = '';
    overlay.style.transition = ''; overlay.style.opacity = ''; overlay.style.display = '';
    overlay.classList.remove('hidden');
    setTimeout(() => overlay.classList.remove('opacity-0'), 10);
}

export function closeSidebar() {
    const sidebar = document.getElementById('main-sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    sidebar.classList.add('-translate-x-full');
    sidebar.style.transform = ''; sidebar.style.transition = '';
    overlay.style.transition = ''; overlay.style.opacity = ''; overlay.style.display = '';
    overlay.classList.add('opacity-0');
    setTimeout(() => overlay.classList.add('hidden'), 300);
}

export function toggleSidebar() {
    const isClosed = document.getElementById('main-sidebar').classList.contains('-translate-x-full');
    if (isClosed) openSidebar(); else closeSidebar();
}
