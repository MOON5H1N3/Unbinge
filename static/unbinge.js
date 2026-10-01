/* Unbinge shared JS - toasts, flash banners, and a reusable confirm modal.
 *
 * U8: replaces alert()/confirm() throughout the app with in-app UI.
 * U10: gives every action (save, pause, delete) visible feedback instead of
 * a silent redirect - including when the action FAILED, which previously
 * looked identical to success.
 *
 * S2: wraps window.fetch once, globally, to attach the CSRF token to every
 * POST/PUT/DELETE/PATCH request automatically. The app has 20+ separate
 * fetch() call sites across six templates; adding the header individually
 * to each one would mean 20+ chances to miss one, and a missed one fails
 * silently (a 403 that looks like "something broke") rather than loudly.
 * One shared wrapper means every current and future fetch() call is
 * covered without needing to remember to do anything at each call site.
 */
(function () {
    const originalFetch = window.fetch;
    window.fetch = function (input, init) {
        init = init || {};
        const method = (init.method || 'GET').toUpperCase();
        if (['POST', 'PUT', 'DELETE', 'PATCH'].includes(method) && window.UNBINGE_CSRF_TOKEN) {
            if (init.headers instanceof Headers) {
                if (!init.headers.has('X-CSRFToken')) init.headers.set('X-CSRFToken', window.UNBINGE_CSRF_TOKEN);
            } else {
                init.headers = { 'X-CSRFToken': window.UNBINGE_CSRF_TOKEN, ...(init.headers || {}) };
            }
        }
        return originalFetch(input, init);
    };
})();

function unbingeToast(message, kind = 'info') {
    let stack = document.getElementById('toast-stack');
    if (!stack) {
        stack = document.createElement('div');
        stack.id = 'toast-stack';
        document.body.appendChild(stack);
    }
    const el = document.createElement('div');
    el.className = `toast toast-${kind}`;
    el.textContent = message;
    stack.appendChild(el);

    const remove = () => {
        el.classList.add('toast-leaving');
        setTimeout(() => el.remove(), 180);
    };
    setTimeout(remove, kind === 'error' ? 6000 : 3500);
    el.addEventListener('click', remove);
}

/* Reads ?flash=<message>&flash_kind=<kind> from the URL on page load, shows
 * it as a toast, then strips it from the address bar so a refresh doesn't
 * repeat it. This is what makes edit/pause/delete give feedback after a
 * full-page redirect, without needing a session-based flash store. */
function unbingeShowFlashFromQuery() {
    const params = new URLSearchParams(window.location.search);
    const msg = params.get('flash');
    if (!msg) return;
    const kind = params.get('flash_kind') || 'info';
    unbingeToast(decodeURIComponent(msg), kind);
    params.delete('flash');
    params.delete('flash_kind');
    const clean = window.location.pathname + (params.toString() ? '?' + params.toString() : '');
    window.history.replaceState({}, '', clean);
}
document.addEventListener('DOMContentLoaded', unbingeShowFlashFromQuery);

/* Confirm modal - returns a Promise<boolean>. Usage:
 *   const ok = await unbingeConfirm({title, body, confirmLabel, danger});
 *   if (!ok) return;
 * Escape and backdrop-click both resolve false (U15's fix applied here too,
 * since this modal replaces the ones that lacked it). */
function unbingeConfirm({ title = 'Are you sure?', body = '', confirmLabel = 'Confirm', danger = false } = {}) {
    return new Promise((resolve) => {
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay open';
        overlay.innerHTML = `
            <div class="modal-box" role="dialog" aria-modal="true" aria-labelledby="unbinge-confirm-title">
                <div style="display:flex; justify-content:space-between; align-items:flex-start;">
                    <h3 id="unbinge-confirm-title">${title}</h3>
                    <button type="button" class="modal-close-btn" aria-label="Close">&times;</button>
                </div>
                <p>${body}</p>
                <div class="modal-actions">
                    <button type="button" class="btn-secondary" data-act="cancel">Cancel</button>
                    <button type="button" class="${danger ? 'btn-danger' : 'btn-primary'}" data-act="confirm">${confirmLabel}</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);

        const finish = (result) => {
            overlay.remove();
            document.removeEventListener('keydown', onKey);
            resolve(result);
        };
        const onKey = (e) => { if (e.key === 'Escape') finish(false); };
        document.addEventListener('keydown', onKey);

        overlay.addEventListener('click', (e) => { if (e.target === overlay) finish(false); });
        overlay.querySelector('.modal-close-btn').addEventListener('click', () => finish(false));
        overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => finish(false));
        overlay.querySelector('[data-act="confirm"]').addEventListener('click', () => finish(true));

        overlay.querySelector('[data-act="confirm"]').focus();
    });
}

/* "drip daily" convenience toggle, shared by the promote modal, the edit
 * form, and the queue-a-successor form - all three just pick release
 * day(s), and "daily" is nothing more than all 7 checked at once. There's
 * no separate backend concept for it: checking this box checks (and locks,
 * so it can't drift out of sync by unchecking one day) every checkbox
 * matched by dayCbSelector, and the show still saves as ordinary
 * release_days = "0,1,2,3,4,5,6". Unchecking it unlocks the days again
 * without changing their current state. */
function toggleDailyDrip(dailyCb, dayCbSelector) {
    document.querySelectorAll(dayCbSelector).forEach(cb => {
        cb.checked = dailyCb.checked;
        cb.disabled = dailyCb.checked;
    });
}
