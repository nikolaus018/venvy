// Event wiring without inline handlers. The CSP refuses onclick="…" and
// friends (that is what keeps injected markup from running script), so the
// markup says what to call in data attributes instead:
//
//     <button data-on-click="closeModal('x'); refresh()">
//     <input data-on-enter="save()">            (keydown, Enter only)
//
// A handler is a ;-separated list of calls of Venvy's own global functions.
// Arguments are literals ('str' with JS escapes, numbers, true, false, null),
// `this`, `event`, or a property path off them (this.dataset.filename).
// Nothing is evaluated: the text is parsed here, and only functions defined
// by Venvy's scripts can be called — never a browser built-in such as fetch
// or open, so injected markup can't use this to reach them either.
//
// Listeners are put on the elements themselves (not delegated), as inline
// handlers were, so stopPropagation() and non-bubbling events like blur keep
// behaving the same. Loaded first in <head>: a MutationObserver wires up
// everything the parser and later innerHTML calls add.
(function () {
    const TYPES = ['click', 'keydown', 'change', 'input', 'blur', 'mousemove', 'enter'];
    const SELECTOR = TYPES.map(t => `[data-on-${t}]`).join(',');
    const NATIVE = /\{\s*\[native code\]\s*\}\s*$/;
    const BAD_PROPS = new Set(['constructor', 'prototype', '__proto__']);
    const parsed = new Map();

    function parse(src) {
        let i = 0;
        const fail = (what) => { throw new SyntaxError(`${what} at ${i} in data-on handler: ${src}`); };
        const ws = () => { while (i < src.length && /\s/.test(src[i])) i++; };
        const ident = () => {
            const m = /^[A-Za-z_$][\w$]*/.exec(src.slice(i));
            if (!m) fail('name expected');
            i += m[0].length;
            return m[0];
        };
        const hex = (n) => {
            const h = src.substr(i, n);
            if (h.length !== n || !/^[0-9a-fA-F]+$/.test(h)) fail('bad escape');
            i += n;
            return String.fromCharCode(parseInt(h, 16));
        };
        const str = (q) => {
            let s = '';
            i++;
            while (i < src.length && src[i] !== q) {
                const c = src[i++];
                if (c !== '\\') { s += c; continue; }
                const e = src[i++];
                if (e === 'x') s += hex(2);
                else if (e === 'u') s += hex(4);
                else s += { n: '\n', t: '\t', r: '\r', 0: '\0' }[e] ?? e;
            }
            if (src[i] !== q) fail('unterminated string');
            i++;
            return s;
        };
        const arg = () => {
            ws();
            const c = src[i];
            if (c === "'" || c === '"') return { v: str(c) };
            const num = /^-?\d+(?:\.\d+)?/.exec(src.slice(i));
            if (num) { i += num[0].length; return { v: Number(num[0]) }; }
            const id = ident();
            if (id === 'true') return { v: true };
            if (id === 'false') return { v: false };
            if (id === 'null') return { v: null };
            if (id !== 'this' && id !== 'event') fail(`unknown value ${id}`);
            const props = [];
            while (src[i] === '.') {
                i++;
                const p = ident();
                if (BAD_PROPS.has(p)) fail(`property ${p}`);
                props.push(p);
            }
            return { root: id, props };
        };
        const calls = [];
        ws();
        while (i < src.length) {
            const fn = ident();
            ws();
            if (src[i++] !== '(') fail('( expected');
            const args = [];
            ws();
            if (src[i] !== ')') {
                for (;;) {
                    args.push(arg());
                    ws();
                    if (src[i] !== ',') break;
                    i++;
                }
            }
            if (src[i++] !== ')') fail(') expected');
            ws();
            if (src[i] === ';') i++;
            ws();
            calls.push({ fn, args });
        }
        return calls;
    }

    function run(src, el, event) {
        let calls = parsed.get(src);
        if (!calls) { calls = parse(src); parsed.set(src, calls); }
        for (const { fn, args } of calls) {
            const f = Object.prototype.hasOwnProperty.call(window, fn) ? window[fn] : undefined;
            if (typeof f !== 'function' || NATIVE.test(Function.prototype.toString.call(f))) {
                throw new ReferenceError(`data-on handler: ${fn} is not one of Venvy's functions`);
            }
            f(...args.map(a => 'v' in a ? a.v : a.props.reduce((o, p) => (o == null ? o : o[p]), a.root === 'this' ? el : event)));
        }
    }

    const wired = new WeakMap();   // element -> Set of wired types
    function wire(el) {
        let done = wired.get(el);
        for (const type of TYPES) {
            const attr = 'data-on-' + type;
            if (!el.hasAttribute(attr) || (done && done.has(type))) continue;
            if (!done) { done = new Set(); wired.set(el, done); }
            done.add(type);
            el.addEventListener(type === 'enter' ? 'keydown' : type, function (event) {
                if (type === 'enter' && event.key !== 'Enter') return;
                const src = el.getAttribute(attr);   // read each time: it may have changed
                if (src) run(src, el, event);
            });
        }
    }
    function wireTree(node) {
        if (node.nodeType !== 1) return;
        wire(node);
        node.querySelectorAll(SELECTOR).forEach(wire);
    }

    new MutationObserver(records => {
        for (const r of records) {
            if (r.type === 'attributes') wire(r.target);
            else r.addedNodes.forEach(wireTree);
        }
    }).observe(document.documentElement, {
        childList: true, subtree: true, attributes: true, attributeFilter: TYPES.map(t => 'data-on-' + t),
    });
    document.addEventListener('DOMContentLoaded', () => wireTree(document.documentElement));

    // Small helpers for handlers that used to be a line of inline script.
    window.focusEl = function focusEl(id) { document.getElementById(id)?.focus(); };
    window.clickEl = function clickEl(id) { document.getElementById(id)?.click(); };
    window.clearValue = function clearValue(id) { const el = document.getElementById(id); if (el) el.value = ''; };
    window.reloadPage = function reloadPage() { location.reload(); };
    window.toggleSecretVis = function toggleSecretVis(id) {
        const el = document.getElementById(id);
        if (el) el.type = el.type === 'password' ? 'text' : 'password';
    };
    // copyText() (feedback.js) with the value of an input, or an element's text.
    window.copyFrom = function copyFrom(id, message) {
        const el = document.getElementById(id);
        if (el) copyText('value' in el ? el.value : el.textContent, message);
    };
})();
