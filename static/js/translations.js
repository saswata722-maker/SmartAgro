const LANGUAGES = [
    { code: 'en', name: 'English', flag: '🇬🇧', label: 'EN' },
    { code: 'hi', name: 'हिन्दी (Hindi)', flag: '🇮🇳', label: 'HI' },
    { code: 'bn', name: 'বাংলা (Bengali)', flag: '🇮🇳', label: 'BN' },
    { code: 'te', name: 'తెలుగు (Telugu)', flag: '🇮🇳', label: 'TE' },
    { code: 'mr', name: 'मराठी (Marathi)', flag: '🇮🇳', label: 'MR' },
    { code: 'ta', name: 'தமிழ் (Tamil)', flag: '🇮🇳', label: 'TA' },
    { code: 'gu', name: 'ગુજરાતી (Gujarati)', flag: '🇮🇳', label: 'GU' },
    { code: 'kn', name: 'ಕನ್ನಡ (Kannada)', flag: '🇮🇳', label: 'KN' },
    { code: 'ml', name: 'മലയാളം (Malayalam)', flag: '🇮🇳', label: 'ML' },
    { code: 'pa', name: 'ਪੰਜਾਬੀ (Punjabi)', flag: '🇮🇳', label: 'PA' },
    { code: 'or', name: 'ଓଡ଼ିଆ (Odia)', flag: '🇮🇳', label: 'OR' },
    { code: 'as', name: 'অসমীয়া (Assamese)', flag: '🇮🇳', label: 'AS' },
    { code: 'ur', name: 'اردو (Urdu)', flag: '🇮🇳', label: 'UR' },
    { code: 'mai', name: 'मैथिली (Maithili)', flag: '🇮🇳', label: 'MAI' },
    { code: 'sat', name: 'संताली (Santali)', flag: '🇮🇳', label: 'SAT' },
    { code: 'ks', name: 'کٲشُر (Kashmiri)', flag: '🇮🇳', label: 'KS' },
    { code: 'ne', name: 'नेपाली (Nepali)', flag: '🇮🇳', label: 'NE' },
    { code: 'sd', name: 'سنڌي (Sindhi)', flag: '🇮🇳', label: 'SD' },
    { code: 'kok', name: 'कोंकणी (Konkani)', flag: '🇮🇳', label: 'KOK' },
    { code: 'mni', name: 'মণিপুরী (Manipuri)', flag: '🇮🇳', label: 'MNI' },
    { code: 'bodo', name: 'बोडो (Bodo)', flag: '🇮🇳', label: 'BDO' },
    { code: 'doi', name: 'डोगरी (Dogri)', flag: '🇮🇳', label: 'DOI' },
    { code: 'sa', name: 'संस्कृत (Sanskrit)', flag: '🇮🇳', label: 'SA' },
];

/* ════════════════════════════════════════════════
   STATIC UI TRANSLATIONS
   Each language has ALL keys; missing ones fall
   back to English via translate().
════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════════════════════
// Lazy per-language dictionaries
// ──────────────────────────────────────────────────────────────────────────────
// The full 23-language dictionary used to ship inline here (~150 KB). It now
// lives in per-language bundles at /static/js/translations_data/<lang>.js and
// is loaded into `window.__T` only when that language is first selected.
// `en` is loaded eagerly by the templates so the UI always has a fallback.
const T = window.__T || (window.__T = {});

function _loadLanguageText(langCode) {
    if (T[langCode]) return Promise.resolve(T[langCode]);
    return new Promise(function (resolve, reject) {
        const s = document.createElement('script');
        s.src = '/static/js/translations_data/' + encodeURIComponent(langCode) + '.js';
        s.onload = function () {
            if (T[langCode]) resolve(T[langCode]);
            else reject(new Error('language bundle missing: ' + langCode));
        };
        s.onerror = function () { reject(new Error('failed to load language: ' + langCode)); };
        document.head.appendChild(s);
    });
}

function ensureLanguage(langCode, cb) {
    _loadLanguageText(langCode).then(
        function () { if (typeof cb === 'function') cb(); },
        function () { if (typeof cb === 'function') cb(); }   // fail-open
    );
}

let currentLang = localStorage.getItem('agrosmart_lang') || 'en';
window.currentLang = currentLang; // ← ADD

/** Translate a key, falling back to English */
function translate(key) {
    return (T[currentLang] && T[currentLang][key]) || T.en[key] || key;
}

/** Shorthand used by dynamic JS files */
function t(key) { return translate(key); }

function applyTranslations() {
    document.querySelectorAll('[data-translate]').forEach(el => {
        const key = el.getAttribute('data-translate');
        const text = translate(key);
        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
            el.placeholder = text;
        } else if (el.children.length > 0 && !el.classList.contains('nav-item')) {
            el.childNodes.forEach(node => {
                if (node.nodeType === 3 && node.textContent.trim()) {
                    node.textContent = text;
                }
            });
        } else {
            el.textContent = text;
        }
    });
    document.querySelectorAll('[data-translate-placeholder]').forEach(el => {
        el.placeholder = translate(el.getAttribute('data-translate-placeholder'));
    });
    document.documentElement.lang = currentLang;
}

function setLanguage(code) {
    currentLang = code;
    window.currentLang = code; // ← ADD
    localStorage.setItem('agrosmart_lang', code);
    ensureLanguage(code, applyTranslations);
    updateLangUI();
    document.dispatchEvent(new CustomEvent('langChanged', { detail: { lang: code } })); // ← ADD
    document.querySelectorAll('.lang-option').forEach(o => {
        o.classList.toggle('active', o.dataset.code === code);
    });
    // Re-render dynamic sections if they are loaded
    if (typeof reRenderDynamic === 'function') reRenderDynamic();
    if (typeof applyDashboardLanguage === 'function') applyDashboardLanguage(code);
}

function updateLangUI() {
    const lang = LANGUAGES.find(l => l.code === currentLang) || LANGUAGES[0];
    const btn = document.getElementById('currentLang');
    if (btn) btn.textContent = lang.label;
}

function buildLangList(filter = '') {
    const list = document.getElementById('langList');
    if (!list) return;
    const f = filter.toLowerCase();
    const filtered = LANGUAGES.filter(l =>
        l.name.toLowerCase().includes(f) || l.code.toLowerCase().includes(f)
    );
    list.innerHTML = filtered.map(l => `
    <div class="lang-option ${l.code === currentLang ? 'active' : ''}"
         data-code="${l.code}"
         onclick="setLanguage('${l.code}'); closeLangDropdown();">
      <span class="lang-flag">${l.flag}</span>
      <span class="lang-name">${l.name}</span>
      <span class="lang-code">${l.label}</span>
    </div>
  `).join('');
}

function closeLangDropdown() {
    const sel = document.getElementById('langSelector');
    if (sel) sel.classList.remove('open');
}

document.addEventListener('DOMContentLoaded', () => {
    buildLangList();
    updateLangUI();
    ensureLanguage(currentLang, applyTranslations);

    const btn = document.getElementById('langBtn');
    const sel = document.getElementById('langSelector');
    if (btn && sel) {
        btn.addEventListener('click', e => {
            e.stopPropagation();
            sel.classList.toggle('open');
        });
        document.addEventListener('click', e => {
            if (!sel.contains(e.target)) sel.classList.remove('open');
        });
    }
    const searchEl = document.getElementById('langSearch');
    if (searchEl) {
        searchEl.addEventListener('input', e => buildLangList(e.target.value));
    }
});
