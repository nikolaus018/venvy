/**
 * User administration: listing, creating and deleting accounts.
 */

import { state } from "../core/state.js";
import { API } from "../core/api.js";
import { escapeHtml, showToast } from "../core/dom.js";
import { closeModal, openModal } from "../ui/modals.js";
import { _featherReplace } from "../core/dom.js";

export const _ROLE_BADGE = {
    admin:    'background:rgba(59,130,246,0.15);color:#60a5fa;border:1px solid rgba(59,130,246,0.2)',
    operator: '',
    viewer:   'background:rgba(148,163,184,0.15);color:#94a3b8;border:1px solid rgba(148,163,184,0.2)',
};

export async function openUsersModal() { openModal('users-modal'); loadUsers(); }

export async function loadUsers() {
    try {
        const res = await fetch(`${API}/users`);
        if (!res.ok) return;
        const users = await res.json();
        const listEl = document.getElementById('users-list');
        listEl.innerHTML = users.map(u => {
            const isSelf = state.currentUser && u.username === state.currentUser.username;
            const badgeStyle = _ROLE_BADGE[u.role] ?? '';
            const roleLabel = u.role ? u.role.charAt(0).toUpperCase() + u.role.slice(1) : 'Operator';
            return `
    <tr class="transition" style="border-color:var(--border)">
        <td class="px-5 py-3.5 font-medium text-sm" style="color:var(--text)">${escapeHtml(u.username)}</td>
        <td class="px-5 py-3.5"><span class="px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide ${u.role === 'operator' ? 'status-stopped' : ''}" style="${badgeStyle}">${roleLabel}</span></td>
        <td class="px-5 py-3.5 text-right">
            ${!isSelf ? `<button data-uid="${u.id}" data-uname="${escapeHtml(u.username)}" class="user-del-btn btn-danger text-xs px-3 py-1.5 rounded-lg font-bold">Delete</button>` : ''}
        </td>
    </tr>`;
        }).join('');
        listEl.querySelectorAll('.user-del-btn').forEach(btn => btn.addEventListener('click', () => deleteUser(Number(btn.dataset.uid), btn.dataset.uname)));
    } catch { showToast('Failed to load users.', 'error'); }
}

export async function createUser() {
    const u = document.getElementById('new-user-name').value.trim();
    const p = document.getElementById('new-user-pwd').value.trim();
    const role = document.getElementById('new-user-role').value;
    if (!u || !p) { showToast('Fill in both username and password.', 'warn'); return; }
    const res = await fetch(`${API}/users`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: u, password: p, role }) });
    if (res.ok) { document.getElementById('new-user-name').value = ''; document.getElementById('new-user-pwd').value = ''; showToast('User created.', 'success'); loadUsers(); }
    else { const e = await res.json().catch(() => ({ detail: 'Failed' })); showToast('Error: ' + (e.detail || 'Failed'), 'error'); }
}

export async function deleteUser(id, username) {
    document.getElementById('delete-user-name').textContent = username || `User #${id}`;
    document.getElementById('delete-user-modal').dataset.uid = id;
    openModal('delete-user-modal');
    _featherReplace();
}

export async function confirmDeleteUser() {
    const id = Number(document.getElementById('delete-user-modal').dataset.uid);
    if (!id) return;
    const btn = document.getElementById('delete-user-confirm-btn');
    btn.disabled = true;
    btn.innerHTML = '<svg class="w-4 h-4 animate-spin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 11-6.219-8.56"/></svg> Deleting…';
    try {
        const res = await fetch(`${API}/users/${id}`, { method: 'DELETE' });
        closeModal('delete-user-modal');
        if (res.ok) { showToast('User deleted.', 'info'); loadUsers(); }
        else { const e = await res.json().catch(() => ({ detail: 'Delete failed' })); showToast('Error: ' + (e.detail || 'Delete failed'), 'error'); }
    } catch {
        closeModal('delete-user-modal');
        showToast('Network error.', 'error');
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i data-feather="trash-2" class="w-4 h-4"></i> Delete User';
        _featherReplace();
    }
}
