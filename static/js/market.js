/* ── State ──────────────────────────────────── */
let allMarketData = {};
let allLocations = [];
let marketChart = null;
let activeChartType = 'line';
let activeFilter = 'all';
let currentChartCity = 'Delhi';

let _translations = {};
const _translationCache = {};
let _translationInProgress = false;

function setTranslations(data) {
    _translations = data || {};
}

function _t(key) {
    if (!key) return '';
    return _translations[key] || key;
}

/** Translate a crop name */
function tCrop(name) {
    return _translations[name] || name;
}

/** Translate a demand label */
function tDemand(demand) {
    return _translations[demand] || demand;
}

/* ══════════════════════════════════════════════
   LANGUAGE DISPLAY NAMES (for overlay label)
══════════════════════════════════════════════ */
const MARKET_LANG_DISPLAY_NAMES = {
    hi: 'हिन्दी',
    bn: 'বাংলা',
    te: 'తెలుగు',
    mr: 'मराठी',
    ta: 'தமிழ்',
    gu: 'ગુજરાતી',
    kn: 'ಕನ್ನಡ',
    ml: 'മലയാളം',
    pa: 'ਪੰਜਾਬੀ',
    or: 'ଓଡ଼ିଆ',
    as: 'অসমীয়া',
    ur: 'اردو',
    mai: 'मैथिली',
    sat: 'ᱥᱟᱱᱛᱟᱴ',
    ks: 'کڲشُر',
    ne: 'नेपाली',
    sd: 'سنڈی',
    kok: 'कोंकणी',
    mni: 'মৈতৈলোন্',
    bodo: 'बड़ो',
    doi: 'डोगरी',
    sa: 'संस्कृत',
    en: 'English',
};

/* ══════════════════════════════════════════════
   TRANSLATE OVERLAY
══════════════════════════════════════════════ */
function ensureMarketTranslateOverlayStyles() {
    if (document.getElementById('marketTranslateOverlayStyle')) return;
    const style = document.createElement('style');
    style.id = 'marketTranslateOverlayStyle';
    style.textContent = `
    .market-translate-overlay {
        position: fixed; inset: 0; z-index: 9999;
        display: flex; align-items: center; justify-content: center;
        background: rgba(10, 16, 12, 0.55);
        backdrop-filter: blur(3px);
        opacity: 0; pointer-events: none;
        transition: opacity 0.2s ease;
    }
    .market-translate-overlay.visible { opacity: 1; pointer-events: all; }
    .market-translate-box {
        background: var(--bg-1, #102013);
        border: 1px solid var(--green, #4ade80);
        border-radius: 16px;
        padding: 28px 32px;
        max-width: 320px;
        text-align: center;
        box-shadow: 0 10px 40px rgba(0,0,0,0.35);
        animation: marketTransPopIn 0.25s ease;
    }
    @keyframes marketTransPopIn {
        from { transform: scale(0.92); opacity: 0; }
        to   { transform: scale(1);    opacity: 1; }
    }
    .market-translate-spinner {
        width: 38px; height: 38px; margin: 0 auto 14px;
        border: 3px solid rgba(74, 222, 128, 0.25);
        border-top-color: var(--green, #4ade80);
        border-radius: 50%;
        animation: marketTransSpin 0.8s linear infinite;
    }
    @keyframes marketTransSpin { to { transform: rotate(360deg); } }
    .market-translate-title {
        color: var(--text-1, #f1f5f1);
        font-weight: 600; font-size: 0.95rem; margin-bottom: 6px;
    }
    .market-translate-sub {
        color: var(--text-3, #94a3a0);
        font-size: 0.78rem; line-height: 1.4;
    }
    .market-translate-dots span {
        display: inline-block; opacity: 0.3;
        animation: marketTransDot 1.2s infinite;
    }
    .market-translate-dots span:nth-child(2) { animation-delay: 0.2s; }
    .market-translate-dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes marketTransDot { 0%,100% { opacity: 0.3; } 50% { opacity: 1; } }
    `;
    document.head.appendChild(style);
}

function showMarketTranslateOverlay(langCode) {
    ensureMarketTranslateOverlayStyles();
    let overlay = document.getElementById('marketTranslateOverlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'marketTranslateOverlay';
        overlay.className = 'market-translate-overlay';
        document.body.appendChild(overlay);
    }
    const name = MARKET_LANG_DISPLAY_NAMES[langCode] || langCode.toUpperCase();
    overlay.innerHTML = `
      <div class="market-translate-box">
        <div class="market-translate-spinner"></div>
        <div class="market-translate-title">Translating to ${name}<span class="market-translate-dots"><span>.</span><span>.</span><span>.</span></span></div>
        <div class="market-translate-sub">First-time translation can take a few seconds. It\'ll be instant after this.</div>
      </div>`;
    requestAnimationFrame(() => overlay.classList.add('visible'));
}

function hideMarketTranslateOverlay() {
    const overlay = document.getElementById('marketTranslateOverlay');
    if (overlay) overlay.classList.remove('visible');
}

async function loadTranslations(lang) {
    if (!lang || lang === 'en') {
        setTranslations({});
        reRenderMarket();
        return;
    }
    if (_translationInProgress) return;
    _translationInProgress = true;

    if (_translationCache[lang]) {
        setTranslations(_translationCache[lang]);
        reRenderMarket();
        _translationInProgress = false;
        return;
    }

    showMarketTranslateOverlay(lang);

    try {
        const res = await fetch('/api/translate-market', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lang }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const tx = data.translations || {};
        _translationCache[lang] = tx; // cache for instant future switches
        setTranslations(tx);
        console.log(`[Market] Translations loaded for ${data.lang_name || lang}: ${Object.keys(_translations).length} terms`);
    } catch (err) {
        console.warn('[Market] Translation load failed, using English:', err);
        setTranslations({});
    }

    // Re-render fully, THEN lift overlay so there's zero gap
    reRenderMarket();
    hideMarketTranslateOverlay();
    _translationInProgress = false;
}

/* ══════════════════════════════════════════════
   INIT
══════════════════════════════════════════════ */
document.addEventListener('DOMContentLoaded', () => {
    loadAllMarkets();
    setupSearchEnterKey();
});

/* ══════════════════════════════════════════════
   DATA LOADING
══════════════════════════════════════════════ */
async function loadAllMarkets() {
    try {
        // Skeleton loader while the (slow, multi-state) market fetch runs (#9)
        showSkeleton('marketCitiesGrid', 4, 'market');
        const res = await fetch('/api/market');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        allMarketData = data.markets || {};
        allLocations = data.locations || Object.keys(allMarketData);

        if (Object.keys(allMarketData).length === 0)
            throw new Error('Empty market data');

        const liveCount = data.live_count || 0;
        const staticCount = data.static_count || 0;
        console.log(`[Market] Live: ${liveCount} | MSP fallback: ${staticCount}`);

        // Show data source badge
        updateDataSourceBadge(liveCount, staticCount);

        hideLoading();
        populateCityDropdown();

        const firstCity = allLocations[0] || 'Delhi';
        currentChartCity = firstCity;
        const sel = document.getElementById('chartCitySelect');
        if (sel) sel.value = firstCity;

        // Apply saved language BEFORE first render so data shows translated right away
        const savedLang = localStorage.getItem('agrosmart_lang') || 'en';
        if (savedLang !== 'en') {
            // Load translations first, then render everything in one pass
            await loadTranslations(savedLang);
        } else {
            renderMarketGrid(allMarketData);
            buildTicker(allMarketData);
            buildChart(allMarketData, firstCity, 'line');
        }

    } catch (err) {
        console.error('[Market] Load error:', err);
        const loader = document.getElementById('marketLoading');
        if (loader) loader.innerHTML = `
            <div style="color:var(--red);text-align:center">
                <i class="fas fa-exclamation-triangle" style="font-size:2rem;margin-bottom:12px"></i>
                <p>Could not load market data. Please refresh.</p>
                <button class="btn-secondary" style="margin-top:16px" onclick="loadAllMarkets()">
                    <i class="fas fa-rotate-right"></i> Retry
                </button>
            </div>`;
    }
}

function updateDataSourceBadge(liveCount, staticCount) {
    const badge = document.getElementById('dataSourceBadge');
    if (!badge) return;
    if (liveCount > 0) {
        badge.innerHTML = `<i class="fas fa-circle" style="color:#4ade80;font-size:0.5rem"></i> ${liveCount} live prices + ${staticCount} MSP reference`;
        badge.style.color = '#4ade80';
    } else {
        badge.innerHTML = `<i class="fas fa-circle" style="color:#fbbf24;font-size:0.5rem"></i> MSP reference prices (live data unavailable)`;
        badge.style.color = '#fbbf24';
    }
}

function hideLoading() {
    const loader = document.getElementById('marketLoading');
    const grid = document.getElementById('marketCitiesGrid');
    if (loader) loader.style.display = 'none';
    if (grid) grid.style.display = '';
}

/* ══════════════════════════════════════════════
   CITY DROPDOWN
══════════════════════════════════════════════ */
function populateCityDropdown() {
    const sel = document.getElementById('chartCitySelect');
    if (!sel) return;
    sel.innerHTML = allLocations
        .map(city => `<option value="${city}">${city}</option>`)
        .join('');
}

/* ══════════════════════════════════════════════
   RENDER MARKET CITY CARDS
══════════════════════════════════════════════ */
function renderMarketGrid(markets) {
    const grid = document.getElementById('marketCitiesGrid');
    const none = document.getElementById('noResults');
    if (!grid) return;

    const entries = Object.entries(markets);
    if (entries.length === 0) {
        grid.style.display = 'none';
        if (none) none.style.display = '';
        return;
    }
    if (none) none.style.display = 'none';
    grid.style.display = '';

    const cropLabel = _t('Crop') || 'Crop';
    const priceLabel = _t('Price') || 'Price';
    const changeLabel = _t('Change') || 'Change';
    const demandLabel = _t('Demand') || 'Demand';
    const cropsLabel = _t('crops') || 'crops';
    // Each city card is an independent scrollable box: ALL of this city's
    // crops render inside the card and the card body scrolls, so a long list
    // never stretches a row and leaves empty space beside shorter cards.

    let hasVisible = false;

    grid.innerHTML = entries.map(([city, crops], cityIdx) => {
                let filtered = crops;
                if (activeFilter === 'Very High') {
                    filtered = crops.filter(c => c.demand === 'Very High');
                } else if (activeFilter === 'rising') {
                    filtered = crops.filter(c => c.change > 0);
                } else if (activeFilter === 'falling') {
                    filtered = crops.filter(c => c.change < 0);
                }
                if (filtered.length === 0) return '';
                hasVisible = true;

                const cardId = `city-card-${cityIdx}`;
                const sourceInfo = filtered[0]?.market ? filtered[0].market : '';
                const arrivalDate = filtered[0]?.arrival_date || '';

                return `
        <div class="city-card" style="animation-delay:${cityIdx * 0.05}s">
            <div class="city-card-header">
                <div class="city-name">
                    <i class="fas fa-location-dot"></i> ${city}
                    ${sourceInfo ? `<span class="city-source-info">${sourceInfo}</span>` : ''}
                </div>
                <div class="city-header-right">
                    <span class="city-count">${filtered.length} ${cropsLabel}</span>
                </div>
            </div>
            <div class="crop-rows-wrapper" id="${cardId}">
                <div class="crop-rows">
                    <div class="crop-row-header" style="display:grid;grid-template-columns:1.5fr 1fr 80px 100px;padding:8px 20px;font-size:0.68rem;color:var(--text-3);font-weight:700;text-transform:uppercase;letter-spacing:0.5px;border-bottom:1px solid var(--border)">
                        <span>${cropLabel}</span>
                        <span>${priceLabel}</span>
                        <span>${changeLabel}</span>
                        <span class="cr-demand-hdr">${demandLabel}</span>
                    </div>
                    ${filtered.map(crop => renderCropRow(crop)).join('')}
                </div>
                ${arrivalDate ? `<div class="city-card-footer"><i class="fas fa-clock"></i> ${_t('Last updated') || 'Last updated'}: ${arrivalDate}</div>` : ''}
            </div>
        </div>`;
    }).join('');

    if (!hasVisible) {
        grid.style.display = 'none';
        if (none) none.style.display = '';
    }

    setTimeout(() => {
        if (typeof observeAnimations === 'function') observeAnimations();
    }, 100);
}

function renderCropRow(crop) {
    const isUp   = crop.change >= 0;
    const pctAbs = Math.abs(crop.change).toFixed(1);
    const marketName = crop.market || '';
    return `
        <div class="crop-row">
            <div class="cr-name-wrap">
                <div class="cr-name" data-crop-key="${crop.crop_key || crop.crop}">${tCrop(crop.crop)}</div>
                ${marketName ? `<div class="cr-market-src"><i class="fas fa-store"></i> ${marketName}</div>` : ''}
            </div>
            <div>
                <div class="cr-price">₹${crop.price.toLocaleString('en-IN')}</div>
                <div class="cr-unit" data-translate-market="quintal">${_t('quintal') || crop.unit}</div>
            </div>
            <div class="cr-change ${isUp ? 'up' : 'down'}">
                <i class="fas fa-arrow-${isUp ? 'up' : 'down'}"></i>
                ${pctAbs}%
            </div>
            <div class="cr-demand demand-${getDemandClass(crop.demand)}" data-demand-key="${crop.demand}">
                ${tDemand(crop.demand)}
            </div>
        </div>`;
}

function getDemandClass(demand) {
    const map = {
        'Very High': 'very-high',
        'High':      'high',
        'Medium':    'medium',
        'Low':       'low',
    };
    return map[demand] || 'medium';
}

/* ══════════════════════════════════════════════
   SEARCH
══════════════════════════════════════════════ */
async function searchLocation() {
    const input    = document.getElementById('locationSearch');
    const clearBtn = document.getElementById('clearSearchBtn');
    if (!input) return;

    const query = input.value.trim();
    if (!query) { clearSearch(); return; }
    if (clearBtn) clearBtn.style.display = 'flex';

    const grid     = document.getElementById('marketCitiesGrid');
    const subtitle = document.getElementById('marketSubtitle');

    if (grid) grid.innerHTML = `
        <div style="grid-column:1/-1;text-align:center;padding:60px 0;">
            <div class="loading-spinner"></div>
            <p style="color:var(--text-2);margin-top:12px;font-size:0.9rem">
                ${_t('Searching') || 'Searching'} <strong style="color:var(--green)">"${query}"</strong>…
            </p>
        </div>`;
    if (subtitle) subtitle.textContent = `${_t('Searching') || 'Searching'} "${query}"…`;

    const q       = query.toLowerCase();
    const matched = Object.fromEntries(
        Object.entries(allMarketData).filter(([city]) => city.toLowerCase().includes(q))
    );

    if (Object.keys(matched).length > 0) {
        renderMarketGrid(matched);
        const firstCity = Object.keys(matched)[0];
        currentChartCity = firstCity;
        buildChart(matched, firstCity, activeChartType);
        if (subtitle) subtitle.textContent = `${_t('Search') || 'Results'}: "${query}"`;
        if (typeof showToast === 'function')
            showToast(`📍 ${Object.keys(matched).length} market(s) found for "${query}"`, 'success');
        return;
    }

    // City not in local cache — try API
    try {
        const res  = await fetch(`/api/market?location=${encodeURIComponent(query)}`);
        const data = await res.json();

        if (!data.markets || Object.keys(data.markets).length === 0) {
            if (grid) grid.style.display = 'none';
            const none = document.getElementById('noResults');
            if (none) none.style.display = '';
        } else {
            renderMarketGrid(data.markets);
            const firstCity = data.locations[0];
            currentChartCity = firstCity;
            buildChart(data.markets, firstCity, activeChartType);
            if (subtitle) subtitle.textContent = `${_t('Search') || 'Showing'}: "${query}"`;
            if (typeof showToast === 'function')
                showToast(`📍 Showing ${firstCity} market data`, 'success');
        }
    } catch {
        if (grid) grid.style.display = 'none';
        const none = document.getElementById('noResults');
        if (none) none.style.display = '';
    }
}

function clearSearch() {
    const input    = document.getElementById('locationSearch');
    const clearBtn = document.getElementById('clearSearchBtn');
    const subtitle = document.getElementById('marketSubtitle');
    const none     = document.getElementById('noResults');
    const grid     = document.getElementById('marketCitiesGrid');

    if (input)    input.value            = '';
    if (clearBtn) clearBtn.style.display = 'none';
    if (subtitle) subtitle.textContent   = _t('Showing all major Indian markets') || 'Showing all major Indian markets';
    if (none)     none.style.display     = 'none';

    if (grid) grid.innerHTML = `
        <div style="grid-column:1/-1;text-align:center;padding:60px 0;">
            <div class="loading-spinner"></div>
            <p style="color:var(--text-2);margin-top:12px;font-size:0.9rem">
                ${_t('Loading markets') || 'Loading markets…'}
            </p>
        </div>`;

    renderMarketGrid(allMarketData);
    buildChart(allMarketData, currentChartCity, activeChartType);
}

function setupSearchEnterKey() {
    const input = document.getElementById('locationSearch');
    if (!input) return;
    input.addEventListener('keydown', e => {
        if (e.key === 'Enter') searchLocation();
    });
    input.addEventListener('input', () => {
        const clearBtn = document.getElementById('clearSearchBtn');
        if (clearBtn) clearBtn.style.display = input.value ? 'flex' : 'none';
    });
}

/* ══════════════════════════════════════════════
   FILTER CHIPS
══════════════════════════════════════════════ */
function filterDemand(type, el) {
    activeFilter = type;
    document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
    if (el) el.classList.add('active');
    renderMarketGrid(allMarketData);
}

/* ══════════════════════════════════════════════
   LIVE PRICE TICKER
══════════════════════════════════════════════ */
function buildTicker(markets) {
    const content = document.getElementById('tickerContent');
    if (!content) return;

    const items = [];
    Object.entries(markets).forEach(([city, crops]) => {
        crops.forEach(crop => {
            const isUp  = crop.change >= 0;
            const sign  = isUp ? '▲' : '▼';
            const color = isUp ? '#4ade80' : '#f87171';
            items.push(
                `<span style="margin:0 28px">
                    <strong style="color:#e8f5e9">${tCrop(crop.crop)}</strong>
                    <span style="color:var(--text-3)">(${city})</span>
                    <strong style="color:var(--amber)"> ₹${crop.price.toLocaleString('en-IN')}</strong>
                    <span style="color:${color};font-size:0.7rem"> ${sign}${Math.abs(crop.change).toFixed(1)}%</span>
                    <span class="cr-demand demand-${getDemandClass(crop.demand)}" style="font-size:0.65rem;padding:1px 6px;border-radius:50px;margin-left:4px">${tDemand(crop.demand)}</span>
                </span>`
            );
        });
    });

    const html = items.join('  •  ');
    content.innerHTML = html + '  •  ' + html;
}

/* ══════════════════════════════════════════════
   CHART CONTROLS
══════════════════════════════════════════════ */
function switchChart(type) {
    activeChartType = type;
    document.querySelectorAll('.chart-tab').forEach(tab => {
        tab.classList.remove('active');
        if (tab.getAttribute('onclick') && tab.getAttribute('onclick').includes(`'${type}'`)) {
            tab.classList.add('active');
        }
    });
    buildChart(allMarketData, currentChartCity, type);
}

function updateChart() {
    const sel = document.getElementById('chartCitySelect');
    if (sel) currentChartCity = sel.value;
    buildChart(allMarketData, currentChartCity, activeChartType);
}

function buildChart(markets, city, type) {
    const canvas = document.getElementById('marketChart');
    if (!canvas) return;

    const cityData = markets[city] || Object.values(markets)[0] || [];
    if (marketChart) { marketChart.destroy(); marketChart = null; }

    if (type === 'line')       buildLineChart(canvas, cityData, city);
    else if (type === 'bar')   buildBarChart(canvas, cityData, city);
    else if (type === 'radar') buildRadarChart(canvas, cityData, city);
}

function interpolateHistory(history, targetLen) {
    if (!history || history.length === 0) return new Array(targetLen).fill(0);
    if (history.length >= targetLen) return history.slice(0, targetLen);

    const result = [];
    const n      = history.length;

    for (let i = 0; i < targetLen; i++) {
        const t    = (i / (targetLen - 1)) * (n - 1);
        const lo   = Math.floor(t);
        const hi   = Math.min(lo + 1, n - 1);
        const frac = t - lo;
        result.push(Math.round(history[lo] * (1 - frac) + history[hi] * frac));
    }
    return result;
}

/* ──────────────────────────────────────────────
   LINE CHART
────────────────────────────────────────────── */
function buildLineChart(canvas, cityData, city) {
    // 30-day date labels (oldest -> today)
    const labels = [];
    for (let i = 29; i >= 0; i--) {
        const d = new Date();
        d.setDate(d.getDate() - i);
        labels.push(d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' }));
    }

    // High-contrast palette so the lines stay easy to tell apart.
    const palette = [
        '#4ade80', '#fbbf24', '#38bdf8', '#a78bfa', '#f87171', '#2dd4bf',
        '#fb923c', '#e879f9', '#84cc16', '#06b6d4', '#ec4899', '#f59e0b',
    ];

    const demandScore = { 'Very High': 4, 'High': 3, 'Medium': 2, 'Low': 1 };
    // Rank by demand first, then by absolute price move, so the crops that
    // matter most to a farmer appear at the top of the chart and legend.
    const sorted = [...cityData].sort((a, b) =>
        (demandScore[b.demand] || 0) - (demandScore[a.demand] || 0) ||
        Math.abs(b.change) - Math.abs(a.change)
    );

    // Only show the top few to keep the chart readable; there are no longer
    // 8+ crossing lines that turn into an unreadable tangle.
    const visibleCount = 6;
    const visible = sorted.slice(0, visibleCount);

    const ref = new Map(); // crop label -> crop object (for tooltips)
    const datasets = visible.map((crop, idx) => {
        const history30 = interpolateHistory(crop.history || [], 30);
        const color     = palette[idx % palette.length];
        ref.set(tCrop(crop.crop), crop);
        return {
            label:           tCrop(crop.crop),
            data:            history30,
            borderColor:     color,
            backgroundColor: color + '14',          // very light fill under the line
            borderWidth:     2.5,
            tension:         0.32,                  // gentle smoothing, not noisy
            fill:            true,
            pointRadius:     ctx => (ctx.dataIndex === history30.length - 1 ? 4 : 0), // dot on "today"
            pointHoverRadius: 6,
            pointBackgroundColor: color,
            pointBorderColor:     '#0e1510',
            pointBorderWidth:     2,
        };
    });

    const titleText = `${city} — ${_t('30-Day Price Trend')} (₹/${_t('quintal') || 'quintal'})`;

    marketChart = new Chart(canvas, {
        type: 'line',
        data: { labels, datasets },
        options: {
            ...getBaseChartOptions(titleText),
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: {
                    grid:   { color: 'rgba(74,222,128,0.05)', drawBorder: false },
                    ticks:  { color: '#6b8c6c', font: { size: 10 }, maxTicksLimit: 8 },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
                y: {
                    grid:   { color: 'rgba(74,222,128,0.06)', drawBorder: false },
                    ticks:  {
                        color:         '#6b8c6c',
                        maxTicksLimit: 7,
                        callback:      v => '₹' + v.toLocaleString('en-IN'),
                    },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
            },
            plugins: {
                ...getBaseChartOptions(titleText).plugins,
                tooltip: {
                    ...getBaseChartOptions(titleText).plugins.tooltip,
                    callbacks: {
                        title: items => {
                            const i = items[0] ? items[0].dataIndex : 0;
                            return labels[i] || '';
                        },
                        label: ctx => {
                            const crop = ref.get(ctx.dataset.label);
                            const sign = crop && crop.change >= 0 ? '▲' : '▼';
                            const chg  = crop ? Math.abs(crop.change).toFixed(1) : '';
                            return ` ${ctx.dataset.label}: ₹${ctx.raw.toLocaleString('en-IN')}` +
                                   (crop ? `  ${sign}${chg}%  (${tDemand(crop.demand)})` : '');
                        },
                    },
                },
            },
        },
    });
}

/* ──────────────────────────────────────────────
   BAR CHART
────────────────────────────────────────────── */
function buildBarChart(canvas, cityData, city) {
    const colors = cityData.map(c =>
        c.change >= 3  ? 'rgba(74,222,128,0.90)'  :
        c.change >= 1  ? 'rgba(74,222,128,0.55)'  :
        c.change > 0   ? 'rgba(74,222,128,0.35)'  :
        c.change > -1  ? 'rgba(251,191,36,0.70)'  :
        c.change > -3  ? 'rgba(251,191,36,0.50)'  :
                         'rgba(248,113,113,0.80)'
    );
    const borderColors = colors.map(c => c.replace(/[\d.]+\)$/, '1)'));

    marketChart = new Chart(canvas, {
        type: 'bar',
        data: {
            labels:   cityData.map(c => tCrop(c.crop)),
            datasets: [{
                label:           `${_t('Price') || 'Price'} (₹/${_t('quintal') || 'quintal'})`,
                data:            cityData.map(c => c.price),
                backgroundColor: colors,
                borderColor:     borderColors,
                borderWidth:     1,
                borderRadius:    6,
                borderSkipped:   false,
            }],
        },
        options: {
            ...getBaseChartOptions(
                `${city} — ${_t('Current Prices')} (₹/${_t('quintal') || 'quintal'})`
            ),
            scales: {
                x: {
                    grid:   { color: 'rgba(74,222,128,0.05)' },
                    ticks:  {
                        color:       '#a7c4a8',
                        font:        { size: 9 },
                        maxRotation: 50,
                        callback: function(val) {
                            const label = this.getLabelForValue(val);
                            return label.length > 12 ? label.slice(0, 11) + '…' : label;
                        },
                    },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
                y: {
                    grid:   { color: 'rgba(74,222,128,0.06)' },
                    ticks:  {
                        color:    '#6b8c6c',
                        callback: v => '₹' + v.toLocaleString('en-IN'),
                    },
                    border: { color: 'rgba(74,222,128,0.1)' },
                },
            },
            plugins: {
                ...getBaseChartOptions('').plugins,
                tooltip: {
                    backgroundColor: '#0e1510',
                    borderColor:     'rgba(74,222,128,0.25)',
                    borderWidth:     1,
                    titleColor:      '#e8f5e9',
                    bodyColor:       '#a7c4a8',
                    padding:         10,
                    callbacks: {
                        label: ctx => {
                            const crop = cityData[ctx.dataIndex];
                            const sign = crop.change >= 0 ? '▲' : '▼';
                            return [
                                ` ₹${ctx.raw.toLocaleString('en-IN')}/${_t('quintal') || 'quintal'}`,
                                ` ${sign} ${Math.abs(crop.change).toFixed(1)}%  |  ${tDemand(crop.demand)} ${_t('Demand') || 'demand'}`,
                            ];
                        },
                    },
                },
            },
        },
    });
}

/* ──────────────────────────────────────────────
   RADAR CHART
────────────────────────────────────────────── */
function buildRadarChart(canvas, cityData, city) {
    const demandScore = { 'Very High': 100, 'High': 75, 'Medium': 50, 'Low': 25 };
    const display     = cityData.slice(0, 12);

    marketChart = new Chart(canvas, {
        type: 'radar',
        data: {
            labels: display.map(c => tCrop(c.crop)),
            datasets: [
                {
                    label:                _t('Demand Intensity') || 'Demand Intensity',
                    data:                 display.map(c => demandScore[c.demand] || 50),
                    backgroundColor:      'rgba(251,191,36,0.12)',
                    borderColor:          'rgba(251,191,36,0.75)',
                    borderWidth:          2,
                    pointBackgroundColor: '#fbbf24',
                    pointBorderColor:     '#fff',
                    pointBorderWidth:     2,
                    pointRadius:          5,
                },
                {
                    label:                _t('Price Momentum') || 'Price Momentum',
                    data:                 display.map(c => Math.min(100, Math.max(0, (c.change + 10) * 5))),
                    backgroundColor:      'rgba(74,222,128,0.10)',
                    borderColor:          'rgba(74,222,128,0.65)',
                    borderWidth:          2,
                    pointBackgroundColor: '#4ade80',
                    pointBorderColor:     '#fff',
                    pointBorderWidth:     2,
                    pointRadius:          4,
                },
            ],
        },
        options: {
            responsive:          true,
            maintainAspectRatio: false,
            scales: {
                r: {
                    min: 0, max: 100,
                    ticks: {
                        color:         'rgba(107,140,108,0.7)',
                        backdropColor: 'transparent',
                        stepSize:      25,
                        font:          { size: 10 },
                    },
                    grid:        { color: 'rgba(74,222,128,0.08)' },
                    angleLines:  { color: 'rgba(74,222,128,0.10)' },
                    pointLabels: { color: '#a7c4a8', font: { size: 10, weight: '600' } },
                },
            },
            plugins: {
                legend: {
                    display: true,
                    labels:  { color: '#a7c4a8', font: { size: 11 }, usePointStyle: true },
                },
                title: {
                    display: true,
                    text:    `${city} — ${_t('Demand Map') || 'Demand Map'}`,
                    color:   '#a7c4a8',
                    font:    { size: 13, weight: '600' },
                    padding: { bottom: 10 },
                },
                tooltip: {
                    backgroundColor: '#0e1510',
                    borderColor:     'rgba(74,222,128,0.25)',
                    borderWidth:     1,
                    titleColor:      '#e8f5e9',
                    bodyColor:       '#a7c4a8',
                },
            },
            animation: { duration: 700, easing: 'easeOutQuart' },
        },
    });
}

/* ──────────────────────────────────────────────
   SHARED BASE CHART OPTIONS
────────────────────────────────────────────── */
function getBaseChartOptions(titleText) {
    return {
        responsive:          true,
        maintainAspectRatio: false,
        interaction:         { mode: 'index', intersect: false },
        plugins: {
            legend: {
                display:  true,
                position: 'bottom',
                labels: {
                    color:           '#a7c4a8',
                    font:            { size: 11 },
                    usePointStyle:   true,
                    pointStyleWidth: 10,
                    boxHeight:       8,
                    padding:         12,
                },
            },
            title: {
                display: !!titleText,
                text:    titleText,
                color:   '#a7c4a8',
                font:    { size: 13, weight: '600' },
                padding: { bottom: 10 },
            },
            tooltip: {
                backgroundColor: '#0e1510',
                borderColor:     'rgba(74,222,128,0.25)',
                borderWidth:     1,
                titleColor:      '#e8f5e9',
                bodyColor:       '#a7c4a8',
                padding:         10,
                callbacks: {
                    label: ctx => ` ${ctx.dataset.label}: ₹${ctx.raw.toLocaleString('en-IN')}`,
                },
            },
        },
        animation: { duration: 700, easing: 'easeOutQuart' },
    };
}

function reRenderMarket() {
    if (Object.keys(allMarketData).length === 0) return;

    const subtitle = document.getElementById('marketSubtitle');
    const search   = document.getElementById('locationSearch');
    if (subtitle && search && !search.value) {
        subtitle.textContent = _t('Showing all major Indian markets') || 'Showing all major Indian markets';
    }

    renderMarketGrid(allMarketData);
    buildTicker(allMarketData);
    buildChart(allMarketData, currentChartCity, activeChartType);
}

document.addEventListener('langChanged', (e) => {
    const lang = e.detail?.lang || 'en';
    loadTranslations(lang);
});
