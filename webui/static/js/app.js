// ─── v1.1.0: CSRF hardening — auto-attach X-Requested-With to state-mutating ──
// fetch calls.  The backend (webui/app.py:_enforce_xhr_header_on_state_changes)
// requires this header on POST/PUT/PATCH/DELETE to ``/api/*`` so a malicious
// cross-origin page cannot trigger pipeline runs / settings rewrites / SSRF
// by initiating a simple-CORS request from the operator's logged-in browser.
// We patch the global ``fetch`` once at module load so every call site —
// including any future code that doesn't explicitly set the header — is
// covered.  The header is harmless for GET / HEAD / OPTIONS (the backend
// only enforces on unsafe methods); we attach it unconditionally for
// API URLs because the cost is negligible and the failure mode (header
// missing → silent 403) is hostile to debug.
(function _installXhrHeaderShim() {
  const _MUTATING = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
  const _origFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      const opts = init || {};
      const method = String(opts.method || (input && input.method) || 'GET').toUpperCase();
      // Resolve URL for the same-origin / api-prefix check.
      let urlStr = '';
      try {
        urlStr = typeof input === 'string' ? input : (input && input.url) || '';
      } catch (_e) { urlStr = ''; }
      if (_MUTATING.has(method) && urlStr) {
        // v1.1.0 third-pass: pathname-strict, same-origin match.
        // The previous ``indexOf('/api/')`` test leaked the header
        // to any third-party endpoint whose URL happened to contain
        // ``/api/`` (mostly harmless, but a privacy leak — the
        // operator's XHR pattern is broadcast to arbitrary origins).
        // Now we resolve the URL against the current origin and
        // require both ``parsed.origin === location.origin`` AND
        // ``parsed.pathname.startsWith('/api/')`` before attaching.
        let isLocalApi = false;
        try {
          const parsed = new URL(urlStr, window.location.origin);
          isLocalApi = parsed.origin === window.location.origin
            && parsed.pathname.startsWith('/api/');
        } catch (_e) {
          // v1.1.0 fourth-pass: fail CLOSED on malformed URLs.  The
          // previous fallback (``urlStr.indexOf('/api/') === 0``)
          // re-introduced the exact privacy leak T18 fixed — a
          // string starting with literal ``/api/`` could be a
          // cross-origin third-party URL.  Failing closed means a
          // mutating fetch to an unparseable URL won't carry the
          // X-Requested-With header — backend rejects with 403,
          // which is easy to debug; the alternative (leak header
          // to arbitrary origin) is a privacy regression.
          isLocalApi = false;
        }
        if (isLocalApi) {
          const headers = new Headers(opts.headers || {});
          if (!headers.has('X-Requested-With')) {
            headers.set('X-Requested-With', 'XMLHttpRequest');
          }
          const newOpts = Object.assign({}, opts, { headers });
          return _origFetch(input, newOpts);
        }
      }
    } catch (_e) { /* fall through to original fetch */ }
    return _origFetch(input, init);
  };
})();

// ─── State ──────────────────────────────────────────────────────────────────────
const State = {
  pages:         { project: { analysisType: 1 }, idea: { analysisType: 1 } },
  sessions:      { project: [], idea: [] },       // Session[] per mode
  activeSession: { project: null, idea: null },   // active sessId per mode
  activeView:    { project: 'terminal', idea: 'terminal' },
  _evtSources:   {},   // sessId → EventSource
  _evtTimers:    {},   // sessId → timer handle
  _elapsedTimers:{},   // sessId → setInterval handle
  charts: {},
  settingsData: {},
  // Leaderboard sort state for client-side secondary sort
  _lbData: [],
  _lbSortCol: null,
  _lbSortAsc: true,
  // Dashboard runs cache for client-side search
  _dashboardRuns: [],
  // Detail modal charts
  _detailChart: null,
  _detailDrawdownChart: null,
  // Tracks the run ID currently being fetched so stale responses are ignored
  _detailRunId: null,
  // Monotonic token so an older runCompare() response cannot overwrite a newer
  // one in #compare-result-wrap (overlapping request pairs race otherwise).
  _compareToken: 0,
  // a11y: element to refocus when the run-detail modal closes (v1.1.11 F-B2)
  _modalLastFocused: null,
};

// ─── Page router ────────────────────────────────────────────────────────────────
const PAGE_META = {
  project:     { title: 'Project Path Mode',   sub: 'Analyze an existing project repository' },
  idea:        { title: 'Idea Mode',            sub: 'Generate code from a natural language description' },
  dashboard:   { title: 'Dashboard',            sub: 'Run history, cost analysis, and agent metrics' },
  leaderboard: { title: 'Leaderboard',          sub: 'Backtest performance ranking across all saved runs' },
  compare:     { title: 'Run Comparison',       sub: 'Side-by-side diff of two saved runs' },
  abtest:      { title: 'Prompt A/B Test',      sub: 'Compare two pipeline configurations side by side' },
  settings:    { title: 'Settings',             sub: 'Configure environment variables and API keys' },
};

// ─── Mobile off-canvas sidebar toggle (≤900px) ───────────────────────────────
// `force`: omit to toggle, pass true/false to set explicitly.  Mirrors state
// onto <body> (for the scrim), the <nav> (slide transform) and the hamburger's
// aria-expanded so AT announces the disclosure state.  (v1.1.11 F-B1)
function toggleSidebar(force) {
  const nav = document.getElementById('primary-sidebar');
  if (!nav) return;
  const open = (typeof force === 'boolean') ? force : !nav.classList.contains('nav-open');
  nav.classList.toggle('nav-open', open);
  document.body.classList.toggle('nav-open', open);
  const btn = document.getElementById('mobile-nav-toggle');
  if (btn) btn.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const pageEl = document.getElementById('page-' + name);
  if (!pageEl) return;  // unknown page guard
  pageEl.classList.add('active');
  const navEl = document.querySelector(`[data-page="${name}"]`);
  if (navEl) navEl.classList.add('active');
  toggleSidebar(false);  // collapse the mobile drawer after navigating (F-B1)
  // Stop the AB-test 4-second poll when the user navigates elsewhere so
  // a forgotten AB session does not keep hitting /api/ab-test/<id> in
  // the background indefinitely (every poll allocates request/response
  // objects that pile up under Chrome's tab-throttling).  The timer is
  // restarted by _initABTest() when the user comes back and runs another
  // comparison.  `typeof` guard keeps this safe if showPage somehow
  // fires before the _ABState const is evaluated.
  if (name !== 'abtest' && typeof _ABState !== 'undefined' && _ABState && _ABState.pollTimer) {
    clearInterval(_ABState.pollTimer);
    _ABState.pollTimer = null;
  }
  const m = PAGE_META[name];
  if (!m) return;  // no meta defined for this page
  document.getElementById('topbar-title').textContent = m.title;
  document.getElementById('topbar-sub').textContent   = m.sub;
  if (name === 'dashboard')   loadDashboard();
  if (name === 'settings')    loadSettings();
  if (name === 'leaderboard') loadLeaderboard();
  if (name === 'compare')     loadCompare();
  if (name === 'abtest')      _initABTest();
}

// ─── Analysis type selector ──────────────────────────────────────────────────────
function selectAnalysis(mode, type, el) {
  document.querySelectorAll(`[data-mode="${mode}"]`).forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
  State.pages[mode].analysisType = type;
  renderFlagGroups(mode, type);
}

// ─── i18n: bilingual desc/tip resolution ──────────────────────────────────────
// FLAG_META / FLAG_GROUPS / EXTENDED_FEATURES_LIST / SETTINGS_FIELDS_META all
// store user-facing strings as either a plain string (legacy / language-neutral)
// or as a {en, zh} object.  getDesc() centralises the language pick so render
// paths can be language-agnostic.  CURRENT_LANG defaults to 'en' but is
// overridden by the persisted WEBUI_LANGUAGE env value (or localStorage cache)
// at startup, see initLanguage().
const LANG_STORAGE_KEY = 'crucible_webui_lang';
const LANG_ENV_KEY     = 'WEBUI_LANGUAGE';
let CURRENT_LANG = 'en';

function getDesc(field) {
  if (field == null) return '';
  if (typeof field === 'string') return field;
  return field[CURRENT_LANG] || field.en || field.zh || '';
}

function _applyLangButtonState() {
  document.querySelectorAll('.lang-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset && btn.dataset.lang === CURRENT_LANG
      || btn.id === ('lang-btn-' + CURRENT_LANG));
  });
}

function setLanguage(lang, opts) {
  if (lang !== 'en' && lang !== 'zh') return;
  if (CURRENT_LANG === lang) { _applyLangButtonState(); return; }
  CURRENT_LANG = lang;
  try { localStorage.setItem(LANG_STORAGE_KEY, lang); } catch (e) { /* private mode etc. */ }
  _applyLangButtonState();
  // Re-render anything that consumes desc/tip so the new language takes effect.
  try { renderFlagGroups('project', getCurrentAnalysisType('project')); } catch (e) {}
  try { renderFlagGroups('idea',    getCurrentAnalysisType('idea'));    } catch (e) {}
  // Settings page only re-renders if the user is currently on it (keeps
  // unrelated XHR off the wire when toggling on Project/Idea pages).
  const settingsPage = document.getElementById('page-settings');
  if (settingsPage && settingsPage.classList.contains('active')) {
    try { loadSettings(); } catch (e) {}
  }
  // Persist to .env unless caller said this is the initial sync (which already
  // came FROM .env and would just write the same value back).
  if (!opts || opts.persist !== false) {
    // v1.1.2 (audit fix G7-C-MED-12): explicit ``X-Requested-With``
    // header on the language-toggle save so the wire format mirrors
    // what backends expect even without the same-origin fetch shim.
    // The shim still injects it for /api/* paths but pinning it
    // explicitly here protects against a future regression where an
    // early-load script uses native XHR or a polyfill that bypasses
    // the shim — without the explicit header that script would
    // silently 403 with no UI signal.
    fetch('/api/env', {
      method:  'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
      },
      body:    JSON.stringify({ [LANG_ENV_KEY]: lang }),
    }).catch(() => { /* non-fatal — localStorage still has it */ });
  }
}

// Helper used by setLanguage() to re-render the visible flag panel with the
// correct analysis type.  Reads from the canonical State.pages store so we
// stay consistent with whatever the user last picked in the analysis-type
// selector.  Falls back to type 1 (Quant) if State isn't initialised yet.
function getCurrentAnalysisType(mode) {
  try {
    const t = State && State.pages && State.pages[mode] && State.pages[mode].analysisType;
    return Number.isInteger(t) ? t : 1;
  } catch (e) { return 1; }
}

async function initLanguage() {
  // 1. Instant restore from localStorage (no network round-trip → no flicker).
  let initial = 'en';
  try {
    const stored = localStorage.getItem(LANG_STORAGE_KEY);
    if (stored === 'en' || stored === 'zh') initial = stored;
  } catch (e) { /* localStorage unavailable */ }
  CURRENT_LANG = initial;
  _applyLangButtonState();
  // 2. Sync from .env so on-disk value wins across devices / fresh installs.
  try {
    const r = await fetch('/api/env');
    if (r.ok) {
      const env = await r.json();
      const fromEnv = env && env[LANG_ENV_KEY];
      if ((fromEnv === 'en' || fromEnv === 'zh') && fromEnv !== CURRENT_LANG) {
        // Update without writing back (we're syncing FROM .env, not TO it).
        setLanguage(fromEnv, { persist: false });
      }
    }
  } catch (e) { /* offline or backend down — keep current */ }
}

// ─── Flag metadata ───────────────────────────────────────────────────────────────
// modes: 'b'=both  'p'=project-only  'i'=idea-only
// types: array of analysis type ints that show this flag (1=Quant 2=SaaS 3=Agent)
const FLAG_META = {
  // Analysis
  dry_run:               { label:'Dry Run',             modes:'p', types:[1,2,3,4], desc:{en:'Scan project context and print a summary without calling any LLM.\nUse case: zero-cost preview of whether context is being captured correctly.', zh:'掃描專案 context 並印出摘要，不呼叫任何 LLM。\n用途：零成本預覽 context 是否正確抓取到。'} },
  self_check:            { label:'Self Check',          modes:'p', types:[1,2,3,4], desc:{en:'Run an offline environment self-check, then exit. No analysis is performed.\nUse case: quick verification that the install and module loading are healthy.', zh:'執行離線環境自我檢查後立即退出，不做任何分析。\n用途：快速確認安裝環境與 module 載入是否正常。'} },
  direction_debate:      { label:'Direction Debate',    modes:'i', types:[1,2,3,4], desc:{en:'Run Stage 0 before the main pipeline: generate 7 directions → Evidence Audit → multi-axis comparison → Judge selects the best.\nUse case: when an idea has multiple viable paths, let the system pick the best direction first.', zh:'主流程前先執行 Stage 0：產生 7 個策略方向 → Evidence Audit → 多軸比較 → Judge 擇優。\n用途：idea 有多條路徑時，讓系統幫你選最佳方向再往下走。'} },
  direction_debate_only: { label:'Debate Only',         modes:'i', types:[1,2,3,4], desc:{en:'Run direction debate only — select the best direction and exit before Analysis Crew.\nUse case: quickly evaluate multiple strategic directions without the full analysis pipeline.', zh:'只執行方向辯論選出最佳方向後立即退出，不進入 Analysis Crew。\n用途：快速評估多個策略方向，不需要完整分析流程。'} },
  // v1.1.8 — Direction Debate Audit Mode per-run toggles.  Both keys are
  // env-backed (see ENV_BACKED_FLAGS) so the checkbox state syncs from the
  // operator's Settings choice on every page load.
  debate_audit_mode:     { label:'Audit Mode',          modes:'i', types:[1,2,3,4], desc:{en:'Enable Direction Debate Audit Mode for this run. Every specialist emits a structured AUDIT_FINDING; the Judge emits a GATE_VERDICT (PROCEED/BRANCH/KILL/NEEDS_MORE_DATA). v1.1.8 is observation-only — the audit ledger captures the disagreement trace but the legacy force-none flow is preserved.\nUse case: capture an auditable disagreement log for v1.2.0 retrieval, or to spot premature consensus on contentious ideas.', zh:'本次執行啟用 Direction Debate Audit Mode。每位 specialist 輸出結構化的 AUDIT_FINDING；Judge 額外輸出 GATE_VERDICT (PROCEED / BRANCH / KILL / NEEDS_MORE_DATA)。v1.1.8 只觀察不覆寫 —— audit ledger 記錄分歧軌跡，但保留 force-none 既有行為。\n用途：為 v1.2.0 retrieval 準備可審計的分歧紀錄，或在有爭議的 idea 上偵測過早共識。'} },
  debate_external_critic:{ label:'External Critic',     modes:'i', types:[1,2,3,4], desc:{en:'Enable the External Critic — a sixth agent that re-judges Judge’s verdict using ONLY raw evidence + Judge’s decision token. Critic does NOT see prior agents’ chain-of-thought. v1.1.8 same-family Critic; cross-family ships in v1.3.0. Critic dissent is recorded in audit_trail but does NOT override Judge unless CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED=1.\nUse case: get a second independent opinion on directions that look too easy or too risky.', zh:'啟用 External Critic —— 第六位 agent，僅使用「原始證據 + Judge 的決策 token」重新審判 Judge 的結論。Critic 看不到其他 agent 的推理過程。v1.1.8 採同模型族 Critic；跨模型族留待 v1.3.0。Critic 反對意見會寫入 audit_trail，但除非 CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED=1 否則不會覆寫 Judge。\n用途：為看起來太容易或太冒險的方向取得獨立的第二意見。'} },
  // v1.1.8 extended — Direction Gate Tuning per-run toggle (degrade-not-die).
  // Backend mirror lives in webui/app.py:_RUN_INSIGHTS_FLAG_TO_ENV; the CLI
  // form is --tolerate-unverifiable-evidence in run_crucible_enhanced.py.
  // Orthogonal to debate_audit_mode — Audit Mode is observation-only, this
  // toggle changes the actual gate decision path.
  debate_tolerate_unverifiable_evidence: { label:'Tolerate Unverifiable Evidence', modes:'i', types:[1,2,3,4], desc:{en:'Allow the direction-debate gate to degrade to low-confidence proceed (instead of force-none) after N consecutive refinement iterations with the same gate reason. ORTHOGONAL to Audit Mode — that one is observation-only; this one changes the actual decision path. Hard feasibility failures are NEVER downgraded.\nUse case: niche topics where Tier-1 cross-validated sources do not exist (e.g. specific crypto market-structure questions). Lets you get a low-confidence direction + actionable critical_unknowns list instead of zero output.', zh:'允許 direction-debate gate 在連續 N 次同原因 force-none 後降為 low-confidence proceed（而非 force-none）。與 Audit Mode 正交 —— Audit Mode 只觀察不覆寫，這個選項會真的改變決策路徑。Hard feasibility 失敗永遠不會降級。\n用途：Tier-1 交叉驗證來源不存在的 niche 主題（例如特定加密貨幣市場結構問題）。用這個拿到 low-confidence 方向 + critical_unknowns 清單，不至於完全沒輸出。'} },
  strict_json:           { label:'Strict JSON',         modes:'b', types:[1,2,3,4], desc:{en:'Force every LLM call to emit schema-conformant strict JSON, with auto-retry on failure.\nUse case: stable output structure for cases where analysis results are consumed by machines.', zh:'強制所有 LLM 呼叫輸出符合 schema 的嚴格 JSON，失敗時自動重試。\n用途：確保輸出結構穩定，適合需要機器讀取分析結果的場合。'} },
  cost_trace:            { label:'Cost Trace',          modes:'b', types:[1,2,3,4], desc:{en:'Emit a cost marker to stderr after every LLM call completes.\nUse case: debug cost anomalies and trace which stage consumes the most.', zh:'每個 LLM 呼叫完成後即時將成本標記輸出到 stderr。\n用途：Debug 成本異常，追蹤哪個階段消耗最多費用。'} },
  cache:                 { label:'Local Cache',         modes:'b', types:[1,2,3,4], desc:{en:'Enable a local SQLite cache so identical LLM calls return cached results instantly.\nUse case: avoid duplicate LLM cost across repeated runs or flag tweaks; iterate much faster.', zh:'啟用 SQLite 本地快取，相同輸入的 LLM 步驟直接讀取快取結果。\n用途：重複執行或微調旗標時避免重複 LLM 費用，大幅加速迭代。'} },
  cost_report:           { label:'Cost Report',         modes:'b', types:[1,2,3,4], desc:{en:'Print a full cost report after the run: per-stage prompt/completion tokens and USD spend.\nUse case: analyse the cost breakdown and identify high-spend stages to optimise.', zh:'執行結束後印出完整成本報告：每階段 prompt / completion token 數與 USD 費用。\n用途：分析成本結構，找出可優化的高耗費階段。'} },
  gate_control:          { label:'Gate Control',        modes:'b', types:[1,2,3,4], desc:{en:'After Analysis Crew finishes, the Gate Controller decides: proceed, refine, or kill the pipeline.\nUse case: only let evidence-backed analysis flow into CodeGen — prevent confident output from weak input.', zh:'Analysis Crew 全部完成後，由 Gate Controller 決策：proceed（通過）、refine（補強）或 kill（中止 pipeline）。\n用途：確保只有證據充分的分析才進入 CodeGen，防止弱輸入產出自信結果。'} },
  selective_rerun:       { label:'Selective Rerun',     modes:'b', types:[1,2,3,4], desc:{en:'When the Gate requests refine, rerun only the named analysts instead of the whole Analysis Crew.\nUse case: save tokens and target the refinement. Requires Gate Control.', zh:'Gate 要求 refine 時，只重跑被點名的特定分析師，而非整個 Analysis Crew。\n用途：節省 token 費用，讓補強更有針對性。需搭配 Gate Control 使用。'} },
  api_version_check:     { label:'API Version Check',   modes:'b', types:[1,2,3,4], desc:{en:'Before CodeGen, auto-check dependency API versions and flag deprecated classes/methods/parameters.\nUse case: ensure generated code uses current APIs, reducing deprecation warnings and compatibility issues.', zh:'CodeGen 前自動檢查依賴套件的 API 版本，標記已棄用的 class / method / 參數。\n用途：確保產出程式碼使用最新 API，減少棄用警告與相容性問題。'} },
  // Code Gen (idea only)
  codegen_auto_optimize: { label:'Auto-Optimize',       modes:'i', types:[1,2,3,4], desc:{en:'After CodeGen, run a generate → critique → refine loop. Each round is scored by codegen_critic; if below threshold, the critique is injected and code regenerated.\nUse case: automatically raise code quality — the system returns the highest-scoring bundle.\nNote: Idea mode only.', zh:'CodeGen 後啟用 generate → critique → refine 迴圈。每輪由 codegen_critic 評分，未達閾值則注入 critique 重新產碼。\n用途：自動提升程式碼品質，系統輸出歷史最高分的 bundle。\n注意：只在 Idea 模式下生效。'} },
  // Post-processing
  use_memory:            { label:'Project Memory',      modes:'b', types:[1,2,3,4], isDefault:true,  desc:{en:'Persist and reload project memory (conclusions, key decisions, known issues) and inject it as extra context on the next run.\nUse case: let the system remember your project history — fewer redundant analyses, higher consistency.', zh:'儲存並載入專案記憶（分析結論、key decisions、已知問題）。下次執行時作為額外 context 注入。\n用途：讓系統記住你的專案歷史，減少重複分析，提高一致性。'} },
  security_scan:         { label:'Security Scan',       modes:'b', types:[1,2,3,4], isDefault:true,  desc:{en:'Run Bandit static security analysis on generated code to surface SQL injection, XSS, hardcoded secrets, etc.\nUse case: baseline security check, ideal for a final review before delivery.', zh:'對產出程式碼執行 Bandit 靜態安全掃描，找出 SQL injection、XSS、硬編碼密鑰等潛在漏洞。\n用途：確保產出程式碼的基本安全品質，適合交付前的最終檢查。'} },
  deployment_artifacts:  { label:'Deployment Artifacts',modes:'b', types:[1,2,3,4], isDefault:true,  desc:{en:'Auto-generate Dockerfile, docker-compose.yml, K8s manifests, and a Helm chart.\nUse case: ship generated code with full containerised deployment artifacts ready to go.', zh:'自動生成 Dockerfile、docker-compose.yml、K8s manifests 與 Helm chart。\n用途：讓產出的程式碼立刻具備可部署的完整容器化設定。'} },
  generate_tests:        { label:'Generate Tests',      modes:'b', types:[1,2,3,4], desc:{en:'Auto-generate a pytest suite covering unit tests for primary functions and integration tests for critical paths.\nUse case: baseline test coverage that reduces regression risk in generated code.', zh:'自動生成 pytest 測試套件，涵蓋主要函式的單元測試與關鍵路徑的整合測試。\n用途：確保產出程式碼有基本測試覆蓋，降低 regression 風險。'} },
  api_autopatch:         { label:'API Autopatch',       modes:'b', types:[1,2,3,4], desc:{en:'Auto-patch deprecated API calls in generated code to the current version of each package.\nUse case: pairs with API Version Check — scan, then patch, with no manual changes.', zh:'自動修補程式碼中已棄用的 API 呼叫，升級到對應套件的最新版本。\n用途：與 API Version Check 搭配使用：掃描後自動修補，無需手動逐一更改。'} },
  independent_validation:{ label:'Indep. Validation',   modes:'b', types:[1,2,3,4], desc:{en:'Use an independent LLM instance to re-validate results and surface contradictions or omissions vs the main flow.\nUse case: improve reliability by avoiding single-LLM echo-chamber bias.', zh:'使用獨立 LLM instance 對產出結果做二次驗證，對照主流程結論找出矛盾或遺漏。\n用途：提高分析可靠性，避免單一 LLM 自我確認偏誤（echo chamber）。'} },
  ci_output:             { label:'CI Output',           modes:'b', types:[1,2,3,4], desc:{en:'Generate GitHub Actions / GitLab CI config with lint, test, build, and deploy jobs.\nUse case: plug generated code straight into a CI/CD pipeline — no hand-written workflow required.', zh:'生成 GitHub Actions / GitLab CI 設定檔，包含 lint、test、build、deploy job。\n用途：讓產出程式碼直接接入 CI/CD pipeline，無需手動編寫 workflow。'} },
  auto_remediation:      { label:'Auto Remediation',    modes:'b', types:[1,2,3,4], desc:{en:'Auto-fix issues surfaced by the security scan and quality analysis, up to N rounds (controlled by ENHANCED_AUTO_REMEDIATION_MAX_ROUNDS).\nUse case: one-click remediation of common issues — drastically less manual intervention.', zh:'自動修復安全掃描與品質分析所發現的問題，最多執行 N 輪修復迴圈（受 ENHANCED_AUTO_REMEDIATION_MAX_ROUNDS 控制）。\n用途：一鍵修復常見問題，大幅減少人工介入。'} },
  dependency_audit:      { label:'Dep. Audit',          modes:'b', types:[1,2,3,4], desc:{en:'Audit requirements.txt with pip-audit to flag known CVEs and outdated dependency versions.\nUse case: dependency safety check — a final security gate before production.', zh:'使用 pip-audit 審計 requirements.txt，找出已知 CVE 漏洞與過時依賴版本。\n用途：確保依賴安全，適合生產環境前的最終安全閘門。'} },
  html_report:           { label:'HTML Report',         modes:'b', types:[1,2,3,4], desc:{en:'Generate a self-contained HTML analysis report with charts, full analyst findings, and code summaries.\nUse case: share or archive analysis results — ready for stakeholder presentation.', zh:'生成可在瀏覽器直接開啟的 HTML 分析報告，包含圖表、完整分析師發現與程式碼摘要。\n用途：方便分享或存檔分析結果，適合向團隊或 stakeholder 展示。'} },
  code_quality:          { label:'Code Quality',        modes:'b', types:[1,2,3,4], desc:{en:'Run ruff (lint) and mypy (type check), report issues, and compute a quality score.\nUse case: ensure generated code follows Python best practices and type-safety standards.', zh:'執行 ruff（lint）與 mypy（型別檢查），回報問題並計算品質分數。\n用途：確保產出程式碼符合 Python 最佳實踐與型別安全標準。'} },
  run_registry:          { label:'Run Registry',        modes:'b', types:[1,2,3,4], desc:{en:'Record this run to a local JSON registry: input summary, cost, quality score, output path.\nUse case: build a run history for comparing flag combinations and prompt versions.', zh:'將本次執行記錄到本地 JSON registry，包含輸入摘要、成本、品質分數、輸出路徑。\n用途：建立執行歷史，方便比較不同旗標組合或 prompt 版本的效果差異。'} },
  // Advanced
  interactive:           { label:'Interactive',         modes:'b', types:[1,2,3,4], desc:{en:'Pause at key decision points and wait for additional terminal input or confirmation from the operator.\nUse case: human-in-the-loop intervention for complex or high-risk analyses.', zh:'在關鍵決策點暫停執行，等待使用者在 terminal 輸入額外指引或確認。\n用途：對複雜或高風險的分析做人工介入，確保 pipeline 方向正確。'} },
  dedup_check:           { label:'Dedup Check',         modes:'b', types:[1,2,3,4], desc:{en:'Compare against recent runs by semantic similarity; if the current input closely matches a prior run, prompt to skip.\nUse case: avoid spending on near-duplicate analyses — save API cost.', zh:'用語義相似度比對近期執行歷史，若本次輸入與最近執行高度相似則提示跳過。\n用途：避免重複花費在幾乎相同的分析上，節省 API 費用。'} },
  backtest_runner:       { label:'Backtest Runner',     modes:'b', types:[1],       desc:{en:'Auto-run a historical backtest pipeline on generated quant strategies: fetch data via yfinance/ccxt → run strategy → compute Sharpe, max drawdown, CAGR, etc.\nUse case: validate backtest feasibility immediately after quant analysis.\nNote: requires yfinance and ccxt to be installed.', zh:'自動對產出的量化策略執行歷史回測 pipeline：yfinance / ccxt 獲取數據 → 執行策略 → 計算 Sharpe、max drawdown、CAGR 等指標。\n用途：量化策略分析後立即驗證 backtest 可行性。\n注意：需先安裝 yfinance 與 ccxt。'} },
  notify:                { label:'Notify',              modes:'b', types:[1,2,3,4], desc:{en:'Push a webhook notification when the run completes (Slack / Discord / custom URL).\nUse case: fire-and-forget long runs — get pinged the moment they finish.\nRequires: configure NOTIFY_*_WEBHOOK_URL in Settings.', zh:'執行完成後透過 Webhook 推送通知，支援 Slack / Discord / 自訂 Webhook URL。\n用途：長時間執行時無需盯著 terminal，完成後立刻收到訊息。\n需要：在 Settings 設定 NOTIFY_*_WEBHOOK_URL。'} },
  post_chat:             { label:'Post Chat',           modes:'b', types:[1,2,3,4], desc:{en:'After analysis completes, open an interactive Q&A mode for follow-up questions about results, code details, or design decisions.\nUse case: explore findings deeper without rerunning the whole pipeline.', zh:'分析完成後啟動互動 Q&A 模式，可針對分析結果、程式碼細節或設計決策繼續提問。\n用途：深入探索分析結果，無需重新執行完整 pipeline 即可釐清細節。'} },
  agent_metrics:         { label:'Agent Metrics',       modes:'b', types:[1,2,3,4], desc:{en:'Collect per-agent performance metrics: LLM latency, token usage, success rate, retry count — written to metrics JSON.\nUse case: profile pipeline bottlenecks; find high-latency or retry-heavy agents to optimise.', zh:'收集每個 agent 的效能指標：LLM 延遲、token 用量、執行成功率、retry 次數，寫入 metrics JSON。\n用途：分析 pipeline 瓶頸，找出高延遲或高 retry 的 agent 進行優化。'} },
  ingest_docs:           { label:'Ingest Docs',         modes:'b', types:[2,3,4],   desc:{en:'Before analysis, ingest documents (PDF / MD / TXT / DOCX) from the specified directory and convert them into pipeline context.\nUse case: load research papers (Scientist mode) or API docs / specs / architecture notes (SaaS/Agent mode).', zh:'在分析前讀取指定目錄的文件（PDF / MD / TXT / DOCX）並轉為 pipeline context。\n用途：Scientist 模式可用來讀入論文 PDF；SaaS/Agent 模式可讀入 API 文件、規格書、架構說明。'} },
  multilang_codegen:     { label:'Multilang Codegen',   modes:'b', types:[2,3],     desc:{en:'Emit the implementation in multiple languages simultaneously (default: TypeScript + Go; customise via the Languages field).\nUse case: produce multi-language SDKs or cross-platform implementations in a single run.', zh:'同時產出多種程式語言的實作（預設 TypeScript + Go，可透過 Languages 欄位自訂）。\n用途：需要多語言 SDK 或跨平台實作時使用，一次執行得到多語言版本。'} },
  // Project mode only
  diff_aware:            { label:'Diff-Aware Mode',     modes:'p', types:[1,2,3,4], desc:{en:'Analyse only files that changed against the base ref (default HEAD~1); other files are read as summaries only.\nUse case: dramatic speedup for PR review — ideal for CI auto-analysis of incremental commits.', zh:'只分析與 base ref（預設 HEAD~1）相比有變動的檔案，其餘檔案僅讀取摘要。\n用途：大幅加快 PR review 速度，適合 CI 環境中自動分析每次 commit 的增量變更。'} },
  // Quant Analytics Suite (Quant mode only)
  quant_analytics:       { label:'Quant Analytics',     modes:'b', types:[1], desc:{en:'Master switch for the full quant analytics suite, including Walk-Forward Validation and statistical significance tests (Permutation Test + Bootstrap CI + Deflated Sharpe Ratio).\nUse case: verify strategy edge is real — rule out overfitting and multiple-testing bias.', zh:'啟用完整量化分析套件的總開關，包含 Walk-Forward Validation 與統計顯著性測試（Permutation Test + Bootstrap CI + Deflated Sharpe Ratio）。\n用途：驗證策略邊際是否真實存在，排除過擬合與多重測試偏差。'} },
  walk_forward:          { label:'Walk-Forward',         modes:'b', types:[1], desc:{en:'Rolling IS/OOS split: per-fold Sharpe decay ratio and OOS consistency score. Requires Quant Analytics.\nUse case: stress-test strategy robustness on out-of-sample data.', zh:'滾動切分 IS/OOS 視窗，計算每折的 Sharpe 衰減比與 OOS 一致性分數。需先啟用 Quant Analytics。\n用途：檢測策略在 out-of-sample 期間的穩健性。'} },
  significance_test:     { label:'Sig. Test',            modes:'b', types:[1], desc:{en:'Permutation test (p-value), Bootstrap 95% CI, and Deflated Sharpe Ratio (DSR) on the backtest return series. Requires Quant Analytics.\nUse case: statistically verify Sharpe remains significant after multiple-testing correction.', zh:'對回測收益序列做排列測試（p-value）、Bootstrap 95% CI 與 Deflated Sharpe Ratio（DSR）。需先啟用 Quant Analytics。\n用途：統計驗證策略 Sharpe 是否在多重測試校正後仍顯著。'} },
  regime_detection:      { label:'Regime Detection',     modes:'b', types:[1], desc:{en:'Detect market regime (bull/bear/range) via rolling-volatility thresholding, SMA trend bands, or Baum-Welch HMM (pure-Python).\nUse case: understand how the strategy behaves across regimes and support regime-filtered signals.', zh:'偵測市場機制（牛市/熊市/震盪）：可選 rolling volatility 閾值法、SMA 趨勢帶法或 Baum-Welch HMM（全純 Python 實作）。\n用途：了解策略在不同市場機制下的表現差異，支援機制過濾信號。'} },
  factor_analysis:       { label:'Factor Analysis',      modes:'b', types:[1], desc:{en:'Pure-Python OLS regression for CAPM alpha/beta; optionally fetch Fama-French 3-factor data for multi-factor exposure.\nUse case: decompose returns into alpha vs beta contributions.', zh:'純 Python OLS 回歸計算 CAPM alpha/beta，可選下載 Fama-French 三因子數據計算多因子暴露。\n用途：分解策略收益來源，區分 alpha 與 beta 貢獻。'} },
  transaction_cost:      { label:'Transaction Cost',     modes:'b', types:[1], desc:{en:'Transaction-cost sensitivity analysis: commission + slippage + bid-ask spread + Kyle-lambda market impact; computes break-even cost level.\nUse case: assess whether the strategy survives realistic trading costs.', zh:'交易成本敏感性分析：佣金 + slippage + 買賣價差 + Kyle lambda 市場衝擊，計算損益平衡成本點。\n用途：評估策略在真實交易成本下的可行性。'} },
  monte_carlo:           { label:'Monte Carlo',          modes:'b', types:[1], desc:{en:'Monte Carlo simulation over 5,000 bootstrap paths: VaR/CVaR, drawdown distribution, plus 2008/2020/2022 historical stress scenarios.\nUse case: quantify tail risk and strategy behaviour under extreme market conditions.', zh:'5000 條 bootstrap 路徑的 Monte Carlo 模擬，計算 VaR/CVaR、最大回撤分佈，以及 2008/2020/2022 歷史壓力情境。\n用途：量化尾部風險與策略在極端市場條件下的表現。'} },
  tearsheet:             { label:'Tearsheet',            modes:'b', types:[1], desc:{en:'Generate a full strategy tearsheet (Markdown + HTML): monthly returns heatmap, drawdown periods, ASCII equity curve — auto-merges every available sub-report.\nUse case: a single document with the complete performance overview, ready to share or archive.', zh:'生成完整策略 Tearsheet：Markdown + HTML，含月度收益熱圖、回撤期列表、ASCII 淨值曲線，自動整合所有可用子報告。\n用途：一份文件呈現策略完整績效概覽，方便分享或歸檔。'} },
  signal_analysis:       { label:'Signal Decay',         modes:'b', types:[1], desc:{en:'Per-horizon (1/2/3/5/10/20/40 days) forward-return t-stat with exponential-decay fit to estimate signal half-life.\nUse case: measure how long an edge persists — informs holding period and rebalance frequency.', zh:'逐 horizon（1/2/3/5/10/20/40 日）計算前向報酬 t-stat，擬合指數衰減估算信號半衰期。\n用途：衡量策略邊際的持續時間，幫助決定持倉週期與再平衡頻率。'} },
  risk_attribution:      { label:'Risk Attribution',     modes:'b', types:[1], desc:{en:'Component VaR, Marginal VaR, diversification benefit, and HHI concentration score.\nUse case: quantify each strategy/asset contribution to portfolio risk and support risk-budget allocation.', zh:'計算 Component VaR、Marginal VaR、分散化收益與 HHI 集中度評分。\n用途：量化每個策略/資產對組合風險的貢獻，支援風險預算分配。'} },
  cointegration:         { label:'Cointegration',        modes:'b', types:[1], desc:{en:'Pure-Python ADF cointegration test (MacKinnon 1994 critical values, AIC lag selection) + OLS hedge ratio + spread half-life + Z-score signal (BUY/SELL/HOLD).\nUse case: identify pairs-trade candidates and generate stat-arb signals.', zh:'純 Python ADF 協整檢定（MacKinnon 1994 臨界值、AIC 選階）+ OLS 對沖比率 + spread 半衰期 + Z-Score 信號（BUY/SELL/HOLD）。\n用途：識別可配對交易的資產對，生成統計套利信號。'} },
  dynamic_correlation:   { label:'Dyn. Correlation',     modes:'b', types:[1], desc:{en:'Rolling-window Pearson correlation matrix snapshots + power-iteration PCA (pure Python, no numpy) + diversification score.\nUse case: track how cross-asset correlations evolve over time; detect correlation spikes (de-diversification) as risk events.', zh:'滾動窗口 Pearson 相關矩陣快照 + power iteration PCA（純 Python，無 numpy）+ 分散化評分。\n用途：追蹤資產間相關性的時間演變，偵測相關性驟升（去分散化）等風險事件。'} },
  lockfile_gen:          { label:'Lockfile Gen',         modes:'b', types:[1], desc:{en:'AST-scan the imports of the generated code, resolve package versions, and emit pyproject.toml + requirements.txt + requirements-dev.txt + .python-version.\nUse case: lock the install environment for reproducible deployment to production or CI.', zh:'AST 掃描產出程式碼的 import，解析套件版本，生成 pyproject.toml + requirements.txt + requirements-dev.txt + .python-version。\n用途：確保產出程式碼可重現安裝環境，適合部署至生產或 CI 環境。'} },
  // Run Insights Ledger — per-run toggles for the cross-run telemetry recorder.
  // Each maps to a CRUCIBLE_RUN_INSIGHTS_* env var; checkbox state syncs from
  // /api/env at startup (see _refreshEnvCacheAndRerender) so the panel always
  // shows the real recorder state, not just a hardcoded default.
  run_insights_enabled:       { label:'Run Insights',        modes:'b', types:[1,2,3,4], isDefault:true, desc:{en:'Master switch for the run insights ledger (cross-run telemetry written to .crucible_insights/).\nUse case: turn off all four streams (output / error / debate / params) for a single run without editing .env.\nSyncs from CRUCIBLE_RUN_INSIGHTS_ENABLED on page load.', zh:'Run Insights ledger 總開關（跨 run 遙測，寫入 .crucible_insights/）。\n用途：單次 run 想關掉全部四個 stream（output / error / debate / params）時用，不需改 .env。\n載入時自動從 CRUCIBLE_RUN_INSIGHTS_ENABLED 同步狀態。'} },
  run_insights_record_output: { label:'Record Output',       modes:'b', types:[1,2,3,4], isDefault:true, desc:{en:'Record output_method events after each save_project_output (primary/judge/librarian model IDs, framework, validation verdict, entrypoint, artefact names).\nUse case: lets v1.2.0 retrieval reproduce which model + config produced a given result.', zh:'每次 save_project_output 後紀錄 output_method 事件（primary/judge/librarian 模型 ID、框架、驗證 verdict、entrypoint、產物名）。\n用途：讓 v1.2.0 retrieval 知道某個結果是哪個模型 + 配置產生的，方便重現。'} },
  run_insights_record_errors: { label:'Record Errors',       modes:'b', types:[1,2,3,4], isDefault:true, desc:{en:'Record error_record events when kickoff_crew_with_retry exhausts its retry budget (exception class, first 300 chars of the message, retry count).\nUse case: build a failure history to detect systematic regression vs transient flakes.', zh:'kickoff_crew_with_retry 重試次數用盡時紀錄 error_record 事件（exception class、訊息前 300 字元、重試次數）。\n用途：建立失敗歷史，分辨系統性回歸 vs 偶發失敗。'} },
  run_insights_record_debate: { label:'Record Debate',       modes:'b', types:[1,2,3,4], isDefault:true, desc:{en:'Record direction_debate_rejection events when Stage 0 force-none gate trips or fallback parsing fails.\nUse case: track which directions get rejected so v1.2.0 retrieval can avoid suggesting them again.', zh:'Stage 0 force-none gate 觸發或 fallback parsing 失敗時紀錄 direction_debate_rejection 事件。\n用途：追蹤哪些方向被拒絕，讓 v1.2.0 retrieval 避免再次推薦相同方向。'} },
  run_insights_redact:        { label:'Redact Secrets',      modes:'b', types:[1,2,3,4], isDefault:true, desc:{en:'Walk every event payload recursively and redact API keys, bearer tokens, OAuth secrets, and password-like fields before writing to disk.\nUse case: safe to keep on for shared dev machines or before pushing the archive to cloud storage.', zh:'寫檔前遞迴遍歷每筆 event payload，遮蓋 API key、bearer token、OAuth secret、password 類欄位。\n用途：共用開發機或上傳 archive 到雲端前都應該保持開啟。'} },
};

// Extended Modules — 25 optional post-processing modules rendered as a
// checkbox grid.  Checked names are joined as a comma-separated string and
// sent to the runner via the --v169-features CLI flag (kept for API
// stability — the underlying flag name is internal contract between the
// WebUI and the pipeline backend).
const EXTENDED_FEATURES_LIST = [
  { key:'model_cascade',       label:'Model Cascade',        desc:{en:'Auto-route between cheap/mid/premium models based on token volume and quality score to save cost.', zh:'根據 token 用量與品質分數自動路由至 cheap/mid/premium 模型，節省成本。'} },
  { key:'semantic_cache',      label:'Semantic Cache',        desc:{en:'Compare against past runs by TF-IDF semantic similarity; cache hits return prior results to avoid redundant compute.', zh:'TF-IDF 語意相似度比對歷史 run，命中則回傳快取結果，避免重複計算。'} },
  { key:'few_shot_injector',   label:'Few-Shot Injector',     desc:{en:'Extract examples from high-scoring past runs and auto-inject them into prompts for output consistency.', zh:'從歷史高分 run 提取範例自動注入 prompt，提升輸出一致性。'} },
  { key:'global_knowledge_base',label:'Knowledge Base',       desc:{en:'Accumulate cross-run insights in a global JSONL knowledge base for future runs to reference.', zh:'跨 run 累積洞察至全域 JSONL 知識庫，供後續 run 參考。'} },
  { key:'llm_quality_scorer',  label:'LLM Quality Scorer',    desc:{en:'Call an LLM to score the output 0–100 for quality and record it in the report.', zh:'呼叫 LLM 對產出進行 0–100 品質評分並寫入報告。'} },
  { key:'options_analyzer',    label:'Options Analyzer',      desc:{en:'Black-Scholes pricing with full Greeks analysis (delta/gamma/theta/vega/rho).', zh:'Black-Scholes 定價與 Greeks 分析（delta/gamma/theta/vega/rho）。'} },
  { key:'alt_data_connectors', label:'Alt Data Connectors',   desc:{en:'Connect alternative data sources (news sentiment, on-chain data, social signals) and write into the report.', zh:'接入替代資料源（新聞情緒、鏈上資料、社群指標）並寫入報告。'} },
  { key:'market_stream',       label:'Market Stream',         desc:{en:'Capture real-time or historical market data snapshots and attach them to the analysis output.', zh:'擷取即時或歷史市場資料快照並附加至分析輸出。'} },
  { key:'trading_platform',    label:'Trading Platform',      desc:{en:'Generate trading-platform integration scaffolding (Alpaca / IBKR / Binance).', zh:'生成交易平台整合設定（Alpaca / IBKR / Binance）。'} },
  { key:'scheduler',           label:'Scheduler',             desc:{en:'Generate scheduling scaffolding: APScheduler runner + systemd unit + Windows Task XML.', zh:'產生 APScheduler runner + systemd unit + Windows Task XML 定時排程設定。'} },
  { key:'yaml_pipeline',       label:'YAML Pipeline',         desc:{en:'Serialise the run configuration as a reproducible YAML pipeline definition.', zh:'將 run 設定序列化為可重現的 YAML pipeline 定義檔。'} },
  { key:'multi_project_compare',label:'Multi-Project Compare',desc:{en:'Side-by-side comparison of multiple runs (score, risk, experiments) rendered as an HTML report.', zh:'並排比較多個 run 的分數、風險與實驗結果，生成 HTML 報告。'} },
  { key:'citation_verifier',   label:'Citation Verifier',     desc:{en:'Verify URLs in the analysis report are reachable and that quoted text matches source content.', zh:'驗證分析報告中的 URL 是否可連線，並比對引用文字與來源內容。'} },
  { key:'webhook_templates',   label:'Webhook Templates',     desc:{en:'Generate (and optionally dispatch) webhook payloads for n8n / Zapier / PagerDuty / Slack / Teams.', zh:'生成並（可選）傳送 n8n / Zapier / PagerDuty / Slack / Teams webhook payload。'} },
  { key:'config_wizard',       label:'Config Wizard',         desc:{en:'Auto-recommend and emit an optimised .env config based on the analysis results.', zh:'根據分析結果自動推薦並生成最佳化的 .env 設定檔。'} },
  { key:'prometheus_exporter', label:'Prometheus Exporter',   desc:{en:'Export Prometheus-format metrics (score, cost, risk, etc.) for scraping by monitoring systems.', zh:'輸出 Prometheus 格式 metrics（分數、成本、風險等）供監控系統抓取。'} },
  { key:'grafana_dashboard',   label:'Grafana Dashboard',     desc:{en:'Generate a Grafana JSON dashboard definition with score-trend, cost, and risk panels.', zh:'生成 Grafana JSON dashboard 定義，含分數趨勢、成本與風險面板。'} },
  { key:'redis_cache',         label:'Redis Cache',           desc:{en:'Cache run results in Redis with key normalisation and TTL configuration.', zh:'以 Redis 快取 run 結果，支援 key 正規化與 TTL 設定。'} },
  { key:'celery_worker',       label:'Celery Worker',         desc:{en:'Generate Celery async task definitions and worker startup configuration.', zh:'生成 Celery 非同步任務定義與 worker 啟動設定。'} },
  { key:'auth_manager',        label:'Auth Manager',          desc:{en:'Generate API-key / JWT auth module and route-middleware scaffolding.', zh:'生成 API key / JWT 驗證模組與路由中間件範本。'} },
  { key:'report_annotations',  label:'Report Annotations',    desc:{en:'Auto-annotate the consensus / risk / general sections of the analysis report and write annotations.json.', zh:'為分析報告的共識/風險/一般區塊自動生成結構化標註並寫入 annotations.json。'} },
  { key:'notion_export',       label:'Notion Export',         desc:{en:'Export results as Obsidian-flavoured Markdown and (optionally) create a Notion database page.', zh:'將分析結果寫出為 Obsidian Markdown 檔案，並（可選）建立 Notion 資料庫頁面。'} },
  { key:'chat_bot',            label:'Chat Bot',              desc:{en:'Launch an interactive chat UI for follow-up questions on the analysis (requires LLM API).', zh:'啟動互動式 chat 介面讓使用者對分析結果提問（需要 LLM API）。'} },
  { key:'sandbox_executor',    label:'Sandbox Executor',      desc:{en:'Execute generated code inside a sandboxed environment and capture stdout/stderr.', zh:'在隔離沙盒環境中執行產出的程式碼並擷取輸出與錯誤。'} },
  { key:'type_coverage',       label:'Type Coverage',         desc:{en:'Analyse the Python type-annotation coverage of generated code and produce a report.', zh:'分析產出程式碼的 Python 型別標註覆蓋率並生成報告。'} },
];

const FLAG_GROUPS = [
  { id:'core', title:'Core Settings', icon:'⚡', open:true,
    flags:[], inputs:[],
    selects:[
      { key:'provider', label:'LLM Provider',
        tip:{en:'Choose your LLM provider. OpenRouter supports multi-model routing and USD cost tracking; Alibaba Coding Plan uses token-based billing. Overrides LLM_PROVIDER in .env.', zh:'選擇 LLM 提供商。OpenRouter 支援多模型路由與 USD 成本追蹤；Alibaba Coding Plan 為 token-based 計費。設定後覆蓋 .env 中的 LLM_PROVIDER。'},
        opts:[{v:'',l:'(default from .env)'},{v:'openrouter',l:'OpenRouter'},{v:'alibaba_coding_plan',l:'Alibaba Coding Plan'},{v:'ollama',l:'Ollama (Local)'}] },
      { key:'runtime_profile', label:'Runtime Profile',
        tip:{en:'Lite: lightweight and fast — good for quick validation. Pro (recommended default): balanced quality and cost. Enterprise: top quality with the strongest models — production-grade.', zh:'Lite：輕量快速，適合快速驗證。Pro：建議預設，平衡品質與成本。Enterprise：最高品質，使用最強模型，適合正式生產場景。'},
        opts:[{v:'',l:'(default)'},{v:'lite',l:'Lite'},{v:'pro',l:'Pro'},{v:'enterprise',l:'Enterprise'}] },
      { key:'scope', label:'Code Gen Scope',
        tip:{en:'MVP (default): minimum runnable implementation. Full: complete modular system with risk manager / portfolio / etc. Production: Full + pytest + Dockerfile + GitHub Actions CI — production-ready.', zh:'MVP：最小可執行實作（預設）。Full：完整模組化系統，含 risk manager / portfolio 等。Production：Full + pytest + Dockerfile + GitHub Actions CI，生產就緒。'},
        opts:[{v:'',l:'(default: mvp)'},{v:'mvp',l:'MVP – minimal runnable'},{v:'full',l:'Full – complete modular'},{v:'production',l:'Production – full + Docker + CI'}] },
    ]},
  { id:'analysis',  title:'Analysis Flags',      icon:'🔬', open:false,
    flags:['dry_run','self_check','direction_debate','direction_debate_only',
           'strict_json','cost_trace','cache','cost_report',
           'gate_control','selective_rerun','api_version_check'] },
  { id:'codegen',   title:'Code Generation',     icon:'💻', open:false,
    flags:['codegen_auto_optimize'],
    inputs:[
      { key:'codegen_optimize_rounds',     label:'Max Rounds',          ph:'3',    kind:'int',   modes:'i', types:[1,2,3,4], tip:{en:'Auto-Optimize runs up to N generate→critique→refine rounds (min 1). Stops early once threshold is met; returns the highest-scoring bundle.', zh:'Auto-Optimize 最多執行 N 輪 generate→critique→refine（最小值 1）。提前達到 threshold 時停止，輸出歷史最高分 bundle。'} },
      { key:'codegen_optimize_threshold',  label:'Score Threshold (0–1)',ph:'0.80', kind:'float', modes:'i', types:[1,2,3,4], tip:{en:'Stop early when critic score reaches this threshold (range 0.0–1.0). Default 0.80; recommended range 0.75–0.90.', zh:'Critic 評分達到此閾值時提前停止（範圍 0.0–1.0）。預設 0.80，建議範圍 0.75–0.90。'} },
      { key:'prompt_version_label',        label:'Version Label',        ph:'v1.0', kind:'text',  modes:'b', types:[1,2,3,4], tip:{en:'Tag this run with a version label, recorded in the run registry to compare effects across prompt versions.', zh:'為本次執行貼上版本標籤，記錄到 run registry 以便比較不同 prompt 版本的效果差異。'} },
    ]},
  { id:'budget',    title:'Budget & Limits',     icon:'💰', open:false,
    flags:[],
    inputs:[
      { key:'budget_soft_cost',    label:'Soft Cost Limit (USD)', ph:'blank = env default', kind:'float', modes:'b', types:[1,2,3,4], tip:{en:'Soft cost limit (USD). When reached, the pipeline logs a warning and continues. For monitoring without enforcement.', zh:'軟性成本上限（USD）。達到後印出警告並繼續執行，不中止 pipeline。適合監控但不強制停止。'} },
      { key:'budget_hard_cost',    label:'Hard Cost Limit (USD)', ph:'blank = env default', kind:'float', modes:'b', types:[1,2,3,4], tip:{en:'Hard cost limit (USD). When reached, the pipeline stops immediately and emits the current best result. Prevents accidental overspend.', zh:'硬性成本上限（USD）。達到後立即中止 pipeline，輸出當前最佳結果。防止意外超支。'} },
      { key:'budget_max_tokens',   label:'Max Total Tokens',      ph:'blank = env default', kind:'int',   modes:'b', types:[1,2,3,4], tip:{en:'Cumulative token cap across all LLM calls. Pipeline halts when reached — for strict context-usage control.', zh:'所有 LLM 呼叫的累計 token 上限。達到後中止 pipeline。適合嚴格控制 context 用量的場合。'} },
    ]},
  { id:'post',      title:'Post-Processing',     icon:'🔧', open:true,
    flags:['use_memory','security_scan','deployment_artifacts','generate_tests',
           'api_autopatch','independent_validation','ci_output','auto_remediation',
           'dependency_audit','html_report','code_quality','run_registry'] },
  { id:'advanced',  title:'Advanced Features',   icon:'🚀', open:false,
    flags:['interactive','dedup_check','backtest_runner','notify','post_chat','agent_metrics','ingest_docs','multilang_codegen'],
    inputs:[
      { key:'ingest_docs_dir',   label:'Docs Directory',           ph:'path/to/docs/',          kind:'text',  modes:'b', types:[2,3,4], tip:{en:'Source directory for Ingest Docs. Supports PDF / MD / TXT / DOCX. Scans recursively into subdirectories.', zh:'Ingest Docs 對應的文件目錄路徑，支援 PDF / MD / TXT / DOCX。掃描所有子目錄。'} },
      { key:'multilang_langs',   label:'Languages (comma-sep)',     ph:'typescript,go',          kind:'text',  modes:'b', types:[2,3], tip:{en:'Comma-separated list of languages for Multilang Codegen. Default: typescript,go. Add python, rust, etc.', zh:'Multilang Codegen 要產出的語言列表，逗號分隔。預設 typescript,go。可加 python, rust 等。'} },
      { key:'github_repo',       label:'GitHub Repo (owner/repo)', ph:'openai/openai-python',   kind:'text',  modes:'b', types:[2,3], tip:{en:'GitHub repository to analyse (format: owner/repo). The system fetches README, issues, and structure as context.', zh:'要分析的 GitHub repository（格式 owner/repo）。系統自動抓取 README、issues、結構作為 context。'} },
    ]},
  { id:'run_insights', title:'Run Insights Ledger',   icon:'📚', open:false,
    flags:['run_insights_enabled','run_insights_record_output','run_insights_record_errors',
           'run_insights_record_debate','run_insights_redact'] },
  // v1.1.8 — Direction Debate Audit Mode group.  Only the two boolean
  // toggles (debate_audit_mode, debate_external_critic) are exposed in the
  // per-run panel.  Other audit-mode settings (ISOLATION_MODE select,
  // CONSENSUS_RISK_THRESHOLD float, CRITIC_OVERRIDE_PROCEED boolean) are
  // operator-level decisions and live in the Settings page instead — keeps
  // the per-run panel from sprawling and matches the v1.1.0 design where
  // FLAG_META only supports boolean checkboxes.
  { id:'debate_audit', title:'Direction Debate Audit', icon:'⚖️', open:false,
    flags:['debate_audit_mode','debate_external_critic'] },
  // v1.1.8 extended — Direction Gate Tuning per-run flag panel group.
  // Single env-backed boolean controlling degrade-not-die behaviour.
  { id:'debate_resilience', title:'Direction Gate Tuning', icon:'🛡️', open:false,
    flags:['debate_tolerate_unverifiable_evidence'] },
  { id:'quant',      title:'Quant Analytics Suite',   icon:'📊', open:false,
    flags:['quant_analytics','walk_forward','significance_test','regime_detection',
           'factor_analysis','transaction_cost','monte_carlo','tearsheet',
           'signal_analysis','risk_attribution','cointegration','dynamic_correlation','lockfile_gen'],
    inputs:[
      { key:'regime_method', label:'Regime Method', ph:'volatility', kind:'text', modes:'b', types:[1], tip:{en:'Regime-detection method: volatility (rolling-vol thresholding), trend (SMA bands), or hmm (Baum-Welch HMM). Default: volatility.', zh:'機制偵測方法：volatility（滾動波動率閾值法）、trend（SMA 趨勢帶法）、hmm（Baum-Welch HMM）。預設 volatility。'} },
    ]},
  { id:'extmod', title:'Extended Modules', icon:'✨', open:false,
    flags:[], inputs:[], extendedCheckboxes:true },
  { id:'stage_models', title:'Per-Stage Model Overrides', icon:'🎛️', open:false,
    flags:[],
    inputs:[
      // v1.1.2 (audit fix G7-C-HIGH-3): convert tip strings to bilingual
      // {en, zh} objects so Chinese-mode operators see Chinese tooltips for
      // these three stage-model overrides.  CLAUDE.md § 10's bilingual
      // invariant for KEY_META extends to FLAG_GROUPS.inputs[*].tip and
      // FLAG_GROUPS.selects[*].tip; getDesc() in app.js handles both forms.
      { key:'librarian_model',       label:'Librarian Model',       ph:'(default from .env)', kind:'text', modes:'b', types:[1,2,3,4], tip:{en:'Override OPENROUTER_LIBRARIAN_MODEL / provider librarian model for this run only. Sent as --librarian-model flag.', zh:'僅此次執行覆寫 OPENROUTER_LIBRARIAN_MODEL（或對應 provider 的 librarian model）。會以 --librarian-model 旗標傳入子程序。'} },
      { key:'primary_model',         label:'Analysis Model',        ph:'(default from .env)', kind:'text', modes:'b', types:[1,2,3,4], tip:{en:'Override the primary analysis model for this run only. Sent as --primary-model flag.', zh:'僅此次執行覆寫主要分析模型。會以 --primary-model 旗標傳入子程序。'} },
      { key:'direction_judge_model', label:'Direction Judge Model', ph:'(default from .env)', kind:'text', modes:'b', types:[1,2,3,4], tip:{en:'Override the direction judge model for this run only. Sent as --direction-judge-model flag.', zh:'僅此次執行覆寫 direction judge 模型。會以 --direction-judge-model 旗標傳入子程序。'} },
    ]},
  { id:'diff',      title:'Diff & Version Control', icon:'🔀', open:false,
    flags:['diff_aware'],
    inputs:[
      { key:'diff_base_ref', label:'Base Ref', ph:'HEAD~1', kind:'text', modes:'p', types:[1,2,3,4], tip:{en:'Git ref to diff against (branch, tag, or commit SHA). Default: HEAD~1 (previous commit).', zh:'用於差異比較的 git ref（branch 名、tag 或 commit SHA）。預設 HEAD~1（即上一個 commit）。'} },
    ]},
  { id:'extdata',   title:'External Market Data', icon:'📡', open:false,
    flags:[],
    inputs:[
      { key:'external_data',    label:'Sources (comma-sep)',  ph:'coingecko,fred,alpha_vantage', kind:'text', modes:'b', types:[1], tip:{en:'Comma-separated external data sources. Supports: coingecko (crypto), fred (US macro), alpha_vantage (stock quotes). Configure the corresponding API keys in Settings.', zh:'外部數據源，逗號分隔。支援：coingecko（加密貨幣行情）、fred（美國總經數據）、alpha_vantage（股票報價）。需在 Settings 設定對應 API key。'} },
      { key:'external_symbols', label:'Symbols (comma-sep)',  ph:'BTC,ETH,SP500',                kind:'text', modes:'b', types:[1], tip:{en:'Comma-separated symbols to fetch. Use BTC/ETH for crypto, AAPL/SPY for stocks — exact format depends on the source.', zh:'要拉取的標的代碼，逗號分隔。加密貨幣用 BTC/ETH，股票用 AAPL/SPY。格式依數據源而異。'} },
      { key:'external_start',   label:'Start Date',           ph:'',                             kind:'date', modes:'b', types:[1], tip:{en:'Start date for historical data (YYYY-MM-DD).', zh:'歷史數據的起始日期（YYYY-MM-DD）。'} },
      { key:'external_end',     label:'End Date',             ph:'',                             kind:'date', modes:'b', types:[1], tip:{en:'End date for historical data (YYYY-MM-DD). Defaults to today if blank.', zh:'歷史數據的結束日期（YYYY-MM-DD）。留空預設為今天。'} },
    ]},
];

// ─── /api/env cache for flag-panel sync ─────────────────────────────────────
// The Idea / Path flag panels render BEFORE any /api/env fetch (renderFlagGroups
// is synchronous), so we lazily populate this cache after the first fetch and
// re-render the panels.  Today only the Run Insights ledger flags consult it —
// they map to CRUCIBLE_RUN_INSIGHTS_* env vars (see ENV_BACKED_FLAGS) and the
// user expects toggling them in Settings to sync into the flag panel without
// hard-reloading.  The Settings page maintains its own cache via loadSettings(),
// so this is a separate, smaller cache that only stores what we actually need.
let _ENV_CACHE = {};

// Maps a frontend per-run flag key → backend env var name.  Drives the
// initial checkbox state in the Idea / Path flag panels so toggles that
// the operator has switched on via Settings (or directly in ``.env``)
// render as checked + ON badge in the per-run panel.  Must stay in
// lockstep with ``_FLAG_TO_ENV`` in ``webui/app.py`` — that one drives
// subprocess env overrides at run time.  Mappings verified against actual
// ``env_bool(...)`` / ``os.environ.get(...)`` call sites in the pipeline
// (see ``run_crucible_enhanced.py`` and ``crucible/modules/section_*.py``);
// flags with no env counterpart (pure per-run CLI flags) — ``dry_run``,
// ``self_check``, ``direction_debate``, ``direction_debate_only``,
// ``cost_report``, ``codegen_auto_optimize``, ``diff_aware``, ``notify`` —
// are intentionally NOT listed: they keep their hardcoded ``isDefault``
// behaviour because there is no env value to sync from.
const ENV_BACKED_FLAGS = {
  // Run Insights ledger
  run_insights_enabled:       'CRUCIBLE_RUN_INSIGHTS_ENABLED',
  run_insights_record_output: 'CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT',
  run_insights_record_errors: 'CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS',
  run_insights_record_debate: 'CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE',
  run_insights_redact:        'CRUCIBLE_RUN_INSIGHTS_REDACT',
  // v1.1.8 — Direction Debate Audit Mode per-run toggles.  Backend mirror
  // lives in webui/app.py:_RUN_INSIGHTS_FLAG_TO_ENV — both must stay in
  // lockstep (test_wiring.py verifies the lockstep structurally).
  debate_audit_mode:          'CRUCIBLE_DEBATE_AUDIT_MODE',
  debate_external_critic:     'CRUCIBLE_DEBATE_EXTERNAL_CRITIC',
  // v1.1.8 extended — Direction Gate Tuning (degrade-not-die toggle).
  // Same lockstep rule as the audit-mode mappings above.
  debate_tolerate_unverifiable_evidence: 'CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE',
  // Analysis flags
  strict_json:                'STRICT_JSON',
  cost_trace:                 'COST_TRACE',
  cache:                      'LOCAL_CACHE',
  gate_control:               'GATE_CONTROL_ENABLED',
  selective_rerun:            'SELECTIVE_RERUN_ENABLED',
  api_version_check:          'API_VERSION_CHECK_ENABLED',
  // Post-processing (Enhanced features)
  use_memory:                 'ENHANCED_PROJECT_MEMORY',
  security_scan:              'ENHANCED_SECURITY_SCAN',
  deployment_artifacts:       'ENHANCED_DEPLOYMENT_ARTIFACTS',
  generate_tests:             'ENHANCED_GENERATE_TESTS',
  api_autopatch:              'ENHANCED_API_AUTOPATCH',
  independent_validation:     'ENHANCED_INDEPENDENT_VALIDATION',
  ci_output:                  'ENHANCED_CI_OUTPUT',
  auto_remediation:           'ENHANCED_AUTO_REMEDIATION',
  dependency_audit:           'ENHANCED_DEPENDENCY_AUDIT',
  html_report:                'ENHANCED_HTML_REPORT',
  code_quality:               'ENHANCED_CODE_QUALITY',
  run_registry:               'ENHANCED_RUN_REGISTRY',
  // Advanced features
  interactive:                'ENHANCED_INTERACTIVE',
  dedup_check:                'ENHANCED_DEDUP_CHECK',
  backtest_runner:            'ENHANCED_BACKTEST_RUNNER',
  post_chat:                  'ENHANCED_POST_CHAT',
  agent_metrics:              'ENHANCED_AGENT_METRICS',
  ingest_docs:                'ENHANCED_INGEST_DOCS',
  multilang_codegen:          'ENHANCED_MULTILANG_CODEGEN',
  lockfile_gen:               'ENHANCED_LOCKFILE_GEN',
  // Quant Analytics Suite
  quant_analytics:            'ENHANCED_QUANT_ANALYTICS',
  walk_forward:               'ENHANCED_WALK_FORWARD',
  significance_test:          'ENHANCED_SIGNIFICANCE_TEST',
  regime_detection:           'ENHANCED_REGIME_DETECTION',
  factor_analysis:            'ENHANCED_FACTOR_ANALYSIS',
  transaction_cost:           'ENHANCED_TRANSACTION_COST',
  monte_carlo:                'ENHANCED_MONTE_CARLO',
  tearsheet:                  'ENHANCED_TEARSHEET',
  signal_analysis:            'ENHANCED_SIGNAL_ANALYSIS',
  risk_attribution:           'ENHANCED_RISK_ATTRIBUTION',
  cointegration:              'ENHANCED_COINTEGRATION',
  dynamic_correlation:        'ENHANCED_DYNAMIC_CORRELATION',
};

// Mirror of the Python _env_bool whitelist (see ~/.claude/CLAUDE.md "numerical
// correctness").  Returns true / false for explicit values, null for unset or
// unrecognised values — caller decides the fallback (usually FLAG_META.isDefault).
function _envBoolTruthy(raw) {
  if (raw == null) return null;
  const s = String(raw).trim().toLowerCase();
  if (s === '') return null;
  if (s === '1' || s === 'true' || s === 'yes' || s === 'on')  return true;
  if (s === '0' || s === 'false' || s === 'no'  || s === 'off') return false;
  return null;
}

// For env-backed flags, the env value (when set) wins over FLAG_META.isDefault.
// For all other flags, the hardcoded default is returned unchanged.
function _resolveFlagInitialChecked(key, fallbackDefault) {
  const envKey = ENV_BACKED_FLAGS[key];
  if (envKey) {
    const v = _envBoolTruthy(_ENV_CACHE[envKey]);
    if (v !== null) return v;
  }
  return !!fallbackDefault;
}

// Fetches /api/env once at init, populates _ENV_CACHE, and re-renders both
// flag panels so the previously hardcoded defaults are replaced by the real
// env state.  Failure is non-fatal — the panels keep the FLAG_META defaults.
async function _refreshEnvCacheAndRerender() {
  try {
    const r = await fetch('/api/env');
    if (!r.ok) return;
    const data = await r.json();
    if (data && typeof data === 'object') _ENV_CACHE = data;
  } catch (e) { /* offline / backend down — keep defaults */ }
  try { renderFlagGroups('project', getCurrentAnalysisType('project')); } catch (e) {}
  try { renderFlagGroups('idea',    getCurrentAnalysisType('idea'));    } catch (e) {}
}

// ─── Flag visibility ─────────────────────────────────────────────────────────────
function _flagVisible(itemModes, itemTypes, pageMode, analysisType) {
  const mOk = itemModes === 'b'
    || (itemModes === 'p' && pageMode === 'project')
    || (itemModes === 'i' && pageMode === 'idea');
  return mOk && itemTypes.includes(analysisType);
}

// ─── Render flag groups ───────────────────────────────────────────────────────────
function renderFlagGroups(mode, analysisType) {
  const container = document.getElementById(`flags-${mode}`);
  let html = '';

  for (const grp of FLAG_GROUPS) {
    const visFlags   = (grp.flags   || []).filter(k => {
      const m = FLAG_META[k]; return m && _flagVisible(m.modes, m.types, mode, analysisType);
    });
    const visInputs  = (grp.inputs  || []).filter(i => _flagVisible(i.modes, i.types, mode, analysisType));
    const hasSelects = !!(grp.selects && grp.selects.length);

    if (!visFlags.length && !visInputs.length && !hasSelects && !grp.extendedCheckboxes) continue;

    const openCls = grp.open ? 'open' : '';
    html += `<div class="accordion">
  <div class="accordion-header ${openCls}" onclick="toggleAccordion(this)">
    <span>${grp.icon} ${grp.title}</span><span class="accordion-arrow">▼</span>
  </div>
  <div class="accordion-body ${openCls}">`;

    if (hasSelects) {
      html += '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:4px">';
      grp.selects.forEach(sel => {
        html += `<div class="field">
  <label class="input-label-with-tip">${escHtml(sel.label)}<span class="tip-icon" tabindex="0" role="img" aria-label="${escHtml(getDesc(sel.tip))}" data-tooltip="${escHtml(getDesc(sel.tip))}">?</span></label>
  <select id="${mode}-${sel.key}">`;
        sel.opts.forEach(o => { html += `<option value="${escHtml(o.v)}">${escHtml(o.l)}</option>`; });
        html += '</select></div>';
      });
      html += '</div>';
    }

    if (visFlags.length) {
      html += '<div class="checkbox-grid">';
      visFlags.forEach(k => {
        const m = FLAG_META[k];
        // Env-backed flags (run_insights_*) read their initial state from
        // _ENV_CACHE so the panel reflects the actual recorder configuration
        // rather than a hardcoded default.  All other flags keep the
        // FLAG_META.isDefault behaviour unchanged.
        const initialChecked = _resolveFlagInitialChecked(k, m.isDefault);
        html += cbItem(mode, k, m.label, m.desc, initialChecked);
      });
      html += '</div>';
    }

    if (visInputs.length) {
      html += visFlags.length ? '<div style="margin-top:12px">' : '';
      for (let i = 0; i < visInputs.length; i += 2) {
        const pair = visInputs.slice(i, i + 2);
        html += `<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:10px">`;
        pair.forEach(inp => {
          const inputType = inp.kind === 'date' ? 'date' : (inp.kind === 'float' || inp.kind === 'int') ? 'number' : 'text';
          const extra = inp.kind === 'float' ? 'step="0.01" min="0"' : inp.kind === 'int' ? 'step="1" min="0"' : '';
          html += `<div class="field">
  <label class="input-label-with-tip">${escHtml(inp.label)}<span class="tip-icon" tabindex="0" role="img" aria-label="${escHtml(getDesc(inp.tip))}" data-tooltip="${escHtml(getDesc(inp.tip))}">?</span></label>
  <input type="${inputType}" id="${mode}-${inp.key}" ${extra} placeholder="${escHtml(inp.ph || '')}">
</div>`;
        });
        html += '</div>';
      }
      html += visFlags.length ? '</div>' : '';
    }

    // Extended Modules — render as checkbox grid instead of free-text input.
    // Form key remains v169_features for backend compatibility (--v169-features).
    if (grp.extendedCheckboxes) {
      const bulkLabel  = CURRENT_LANG === 'zh' ? '批次選取：' : 'Bulk:';
      const allLabel   = CURRENT_LANG === 'zh' ? '全選'      : 'Select all';
      const clearLabel = CURRENT_LANG === 'zh' ? '清除'      : 'Clear';
      html += `<input type="hidden" id="${mode}-v169_features">`;
      html += `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
  <span style="font-size:12px;color:var(--text-muted)">${bulkLabel}</span>
  <button type="button" class="btn-xs" onclick="extendedSelectAll('${mode}',true)">${allLabel}</button>
  <button type="button" class="btn-xs" onclick="extendedSelectAll('${mode}',false)">${clearLabel}</button>
</div>`;
      html += '<div class="checkbox-grid">';
      EXTENDED_FEATURES_LIST.forEach(f => {
        const id = `${mode}-extcb-${f.key}`;
        html += `<label class="cb-item" id="cbl-${id}" for="${id}" data-tooltip="${escHtml(getDesc(f.desc))}">
  <input type="checkbox" id="${id}" onchange="onExtendedChange('${mode}')">
  <span>${escHtml(f.label)}</span>
</label>`;
      });
      html += '</div>';
    }

    html += '</div></div>';
  }
  container.innerHTML = html;
}

function cbItem(mode, key, label, desc, isDefault = false) {
  const id = `${mode}-flag-${key}`;
  const defBadge = isDefault ? '<span class="cb-default">ON</span>' : '';
  return `<label class="cb-item${isDefault ? ' checked' : ''}" id="cbl-${id}" for="${id}" data-tooltip="${escHtml(getDesc(desc))}">
  <input type="checkbox" id="${id}" ${isDefault ? 'checked' : ''} onchange="onCbChange(this)">
  <span>${escHtml(label)}</span>${defBadge}
</label>`;
}

function onCbChange(cb) {
  cb.closest('.cb-item').classList.toggle('checked', cb.checked);
}

// Extended-modules checkboxes — sync checked state to the hidden text input.
// The hidden input id stays `v169_features` so the form-collection layer in
// _build_command (webui/app.py) keeps mapping it onto the --v169-features
// CLI flag without churn on the backend contract.
function onExtendedChange(mode) {
  const checked = EXTENDED_FEATURES_LIST
    .filter(f => document.getElementById(`${mode}-extcb-${f.key}`)?.checked)
    .map(f => f.key);
  const hidden = document.getElementById(`${mode}-v169_features`);
  if (hidden) hidden.value = checked.join(',');
  // Mirror .cb-item checked class for consistent styling
  EXTENDED_FEATURES_LIST.forEach(f => {
    const id = `${mode}-extcb-${f.key}`;
    const el = document.getElementById(id);
    const lbl = document.getElementById(`cbl-${id}`);
    if (el && lbl) lbl.classList.toggle('checked', el.checked);
  });
}

function extendedSelectAll(mode, checked) {
  EXTENDED_FEATURES_LIST.forEach(f => {
    const el = document.getElementById(`${mode}-extcb-${f.key}`);
    if (el) el.checked = checked;
  });
  onExtendedChange(mode);
}

function toggleAccordion(header) {
  header.classList.toggle('open');
  header.nextElementSibling.classList.toggle('open');
}

// ─── Collect flags from form ──────────────────────────────────────────────────────
function collectFlags(mode) {
  const flags = {};

  // Selects
  ['provider','runtime_profile','scope'].forEach(k => {
    const el = document.getElementById(`${mode}-${k}`);
    if (el && el.value) flags[k] = el.value;
  });

  // Checkboxes — only collect what exists in DOM (rendered by renderFlagGroups)
  // Send both true (checked) and false (unchecked) so the backend can emit
  // --no-flag for flags that are explicitly turned off by the user, overriding
  // any env-var defaults (e.g. use_memory, security_scan, deployment_artifacts
  // all default to True in the CLI and would silently re-enable if omitted).
  Object.keys(FLAG_META).forEach(k => {
    const el = document.getElementById(`${mode}-flag-${k}`);
    if (el) flags[k] = el.checked;
  });

  // Numeric inputs
  const intKeys   = ['codegen_optimize_rounds','budget_max_tokens'];
  const floatKeys = ['codegen_optimize_threshold','budget_soft_cost','budget_hard_cost'];
  const strKeys   = ['prompt_version_label','ingest_docs_dir','github_repo','multilang_langs',
                     'external_data','external_symbols','external_start','external_end','diff_base_ref',
                     'librarian_model','primary_model','direction_judge_model',
                     'regime_method','v169_features'];

  intKeys.forEach(k => {
    const el = document.getElementById(`${mode}-${k}`);
    if (el && el.value) {
      const n = parseInt(el.value, 10);
      if (!isNaN(n)) flags[k] = n;
    }
  });
  floatKeys.forEach(k => {
    const el = document.getElementById(`${mode}-${k}`);
    if (el && el.value) {
      const n = parseFloat(el.value);
      if (!isNaN(n)) flags[k] = n;
    }
  });
  strKeys.forEach(k => {
    const el = document.getElementById(`${mode}-${k}`);
    if (el && el.value) flags[k] = el.value;
  });

  return flags;
}

// ═══════════════════════════════════════════════════════════════
//  AGENT GRAPH DEFINITIONS
// ═══════════════════════════════════════════════════════════════
const _AF_S = 128, _AF_R = 86, _AF_NW = 104, _AF_NH = 48, _AF_PX = 14, _AF_PY = 18;

const AGENT_GRAPHS = {
  1: { // Quant
    nodes: [
      { id:'dir_seed',      label:'Direction\nSeed Planner', stage:0, row:1 },
      { id:'librarian',     label:'Librarian',               stage:1, row:1 },
      { id:'mkt_res',       label:'Market\nResearch',         stage:2, row:0 },
      { id:'tech_res',      label:'Technical\nResearch',      stage:2, row:1 },
      { id:'comp_res',      label:'Competitor\nResearch',     stage:2, row:2 },
      { id:'synthesizer',   label:'Research\nSynthesizer',    stage:3, row:1 },
      { id:'dir_judge',     label:'Direction\nJudge',         stage:4, row:1 },
      { id:'quant_analyst', label:'Quant\nAnalyst',           stage:5, row:0.5 },
      { id:'risk_mgr',      label:'Risk\nManager',            stage:5, row:1.5 },
      { id:'assembler',     label:'Report\nAssembler',        stage:6, row:1 },
      { id:'gate',          label:'Gate\nController',         stage:7, row:1 },
      { id:'code_arch',     label:'Code\nArchitect',          stage:8, row:0.5 },
      { id:'code_gen',      label:'Code\nGenerator',          stage:8, row:1.5 },
      { id:'self_check',    label:'Self-Check\nValidator',    stage:9, row:1 },
    ],
    edges: [
      ['dir_seed','librarian'],['librarian','mkt_res'],['librarian','tech_res'],['librarian','comp_res'],
      ['mkt_res','synthesizer'],['tech_res','synthesizer'],['comp_res','synthesizer'],
      ['synthesizer','dir_judge'],['dir_judge','quant_analyst'],['dir_judge','risk_mgr'],
      ['quant_analyst','assembler'],['risk_mgr','assembler'],
      ['assembler','gate'],['gate','code_arch'],['gate','code_gen'],
      ['code_arch','self_check'],['code_gen','self_check'],
    ],
  },
  2: { // SaaS
    nodes: [
      { id:'dir_seed',    label:'Direction\nSeed Planner', stage:0, row:1 },
      { id:'librarian',   label:'Librarian',               stage:1, row:1 },
      { id:'mkt_res',     label:'Market\nResearch',         stage:2, row:0 },
      { id:'tech_res',    label:'Technical\nResearch',      stage:2, row:1 },
      { id:'comp_res',    label:'Competitor\nResearch',     stage:2, row:2 },
      { id:'synthesizer', label:'Research\nSynthesizer',    stage:3, row:1 },
      { id:'dir_judge',   label:'Direction\nJudge',         stage:4, row:1 },
      { id:'mkt_analyst', label:'Market\nAnalyst',          stage:5, row:0.5 },
      { id:'tech_arch',   label:'Technical\nArchitect',     stage:5, row:1.5 },
      { id:'assembler',   label:'Report\nAssembler',        stage:6, row:1 },
      { id:'gate',        label:'Gate\nController',         stage:7, row:1 },
      { id:'code_arch',   label:'Code\nArchitect',          stage:8, row:0.5 },
      { id:'code_gen',    label:'Code\nGenerator',          stage:8, row:1.5 },
      { id:'self_check',  label:'Self-Check\nValidator',    stage:9, row:1 },
    ],
    edges: [
      ['dir_seed','librarian'],['librarian','mkt_res'],['librarian','tech_res'],['librarian','comp_res'],
      ['mkt_res','synthesizer'],['tech_res','synthesizer'],['comp_res','synthesizer'],
      ['synthesizer','dir_judge'],['dir_judge','mkt_analyst'],['dir_judge','tech_arch'],
      ['mkt_analyst','assembler'],['tech_arch','assembler'],
      ['assembler','gate'],['gate','code_arch'],['gate','code_gen'],
      ['code_arch','self_check'],['code_gen','self_check'],
    ],
  },
  3: { // Agent
    nodes: [
      { id:'dir_seed',      label:'Direction\nSeed Planner',  stage:0, row:1 },
      { id:'librarian',     label:'Librarian',                stage:1, row:1 },
      { id:'mkt_res',       label:'Market\nResearch',          stage:2, row:0 },
      { id:'tech_res',      label:'Technical\nResearch',       stage:2, row:1 },
      { id:'comp_res',      label:'Competitor\nResearch',      stage:2, row:2 },
      { id:'synthesizer',   label:'Research\nSynthesizer',     stage:3, row:1 },
      { id:'dir_judge',     label:'Direction\nJudge',          stage:4, row:1 },
      { id:'agent_analyst', label:'Agent Systems\nAnalyst',    stage:5, row:0.5 },
      { id:'infra_analyst', label:'Infrastructure\nAnalyst',   stage:5, row:1.5 },
      { id:'assembler',     label:'Report\nAssembler',         stage:6, row:1 },
      { id:'gate',          label:'Gate\nController',          stage:7, row:1 },
      { id:'code_arch',     label:'Code\nArchitect',           stage:8, row:0.5 },
      { id:'code_gen',      label:'Code\nGenerator',           stage:8, row:1.5 },
      { id:'self_check',    label:'Self-Check\nValidator',     stage:9, row:1 },
    ],
    edges: [
      ['dir_seed','librarian'],['librarian','mkt_res'],['librarian','tech_res'],['librarian','comp_res'],
      ['mkt_res','synthesizer'],['tech_res','synthesizer'],['comp_res','synthesizer'],
      ['synthesizer','dir_judge'],['dir_judge','agent_analyst'],['dir_judge','infra_analyst'],
      ['agent_analyst','assembler'],['infra_analyst','assembler'],
      ['assembler','gate'],['gate','code_arch'],['gate','code_gen'],
      ['code_arch','self_check'],['code_gen','self_check'],
    ],
  },
  4: { // Scientist
    nodes: [
      { id:'dir_seed',        label:'Direction\nSeed Planner',  stage:0, row:1 },
      { id:'librarian',       label:'Librarian',                stage:1, row:1 },
      { id:'paper_res',       label:'Paper\nSearch',            stage:2, row:0 },
      { id:'impl_res',        label:'Implementation\nResearch', stage:2, row:1 },
      { id:'baseline_res',    label:'Baseline\nResearch',       stage:2, row:2 },
      { id:'synthesizer',     label:'Research\nSynthesizer',    stage:3, row:1 },
      { id:'dir_judge',       label:'Direction\nJudge',         stage:4, row:1 },
      { id:'paper_researcher',label:'Paper\nResearcher',        stage:5, row:0.5 },
      { id:'algo_analyst',    label:'Algorithm\nAnalyst',       stage:5, row:1.5 },
      { id:'assembler',       label:'Report\nAssembler',        stage:6, row:1 },
      { id:'gate',            label:'Gate\nController',         stage:7, row:1 },
      { id:'code_arch',       label:'Code\nArchitect',          stage:8, row:0.5 },
      { id:'code_gen',        label:'Experiment\nGenerator',    stage:8, row:1.5 },
      { id:'self_check',      label:'Self-Check\nValidator',    stage:9, row:1 },
    ],
    edges: [
      ['dir_seed','librarian'],['librarian','paper_res'],['librarian','impl_res'],['librarian','baseline_res'],
      ['paper_res','synthesizer'],['impl_res','synthesizer'],['baseline_res','synthesizer'],
      ['synthesizer','dir_judge'],['dir_judge','paper_researcher'],['dir_judge','algo_analyst'],
      ['paper_researcher','assembler'],['algo_analyst','assembler'],
      ['assembler','gate'],['gate','code_arch'],['gate','code_gen'],
      ['code_arch','self_check'],['code_gen','self_check'],
    ],
  },
};

// Log-line → agent id patterns (ordered: first match wins)
// IMPORTANT: Specific agent patterns MUST come before broad ones.
// "/librarian/i" was too broad — it matched crew-level log lines like
// "librarian crew: Working Agent: Competitor Research", re-activating
// the librarian node when a research lane agent was actually running.
const AGENT_PATTERNS = [
  { re:/direction.?seed|seed.?plan/i,              id:'dir_seed' },
  // Research lanes BEFORE librarian so "librarian crew: Working Agent: Market Research" matches lane first
  { re:/market.?research|mkt.?res/i,               id:'mkt_res' },
  { re:/technical.?research|tech.?res/i,           id:'tech_res' },
  { re:/competitor.?research|comp.?res/i,          id:'comp_res' },
  { re:/research.?synth|synthesizer/i,             id:'synthesizer' },
  // Librarian: only match when "librarian" is the actual agent, not crew context
  { re:/Working Agent:?\s*Librarian|^\s*Librarian\s*$/i, id:'librarian' },
  { re:/direction.?judge|dir.?judge/i,             id:'dir_judge' },
  { re:/\bexplorer\b/i,                            id:'dir_judge' },
  { re:/\bcomparator\b/i,                          id:'dir_judge' },
  { re:/\bskeptic\b/i,                             id:'dir_judge' },
  { re:/evidence.?auditor/i,                       id:'dir_judge' },
  { re:/\bjudge\b/i,                               id:'dir_judge' },
  { re:/quant.?analyst|quantitative.?analyst/i,    id:'quant_analyst' },
  { re:/risk.?man(ager|mgr)/i,                     id:'risk_mgr' },
  { re:/market.?analyst|mkt.?analyst/i,            id:'mkt_analyst' },
  { re:/technical.?arch|tech.?arch/i,              id:'tech_arch' },
  { re:/agent.?systems?|agent.?analyst/i,          id:'agent_analyst' },
  { re:/infra(structure)?.?analyst/i,              id:'infra_analyst' },
  { re:/paper.?researcher|literature.?analyst/i,     id:'paper_researcher' },
  { re:/algorithm.?analyst|algo.?analyst|experiment.?analyst/i, id:'algo_analyst' },
  { re:/gate.?context.?compact/i,                   id:'assembler' },
  { re:/report.?assembler|assembler/i,             id:'assembler' },
  { re:/gate.?controller/i,                        id:'gate' },
  { re:/code.?architect/i,                         id:'code_arch' },
  { re:/code.?gen(erator)?/i,                      id:'code_gen' },
  // NOTE: `format.?checker` is intentionally NOT in this pattern.  The
  // analysis crew's "Format Checker" agent is a JSON-shape validator that
  // runs at the END of the analysis crew (between gate_controller and the
  // codegen phase), NOT the post-codegen self-check stage.  Including it
  // here previously caused the self_check node to falsely light up during
  // the analysis phase and stay `active` straight into codegen, making it
  // look like an already-done agent was still in use.
  { re:/self.?check|validator/i,                   id:'self_check' },
];

// ═══════════════════════════════════════════════════════════════
//  SESSION MANAGEMENT
// ═══════════════════════════════════════════════════════════════
const _MAX_SESSIONS = 6;
let _sessCounter = { project: 0, idea: 0 };

// Canonical human-readable labels for a session status.  Both the status pill
// (_setSessionStatus) and the terminal header (_updateSessionHeader) resolve
// display text through here so the vocabulary stays consistent across every
// surface (was: pill said "Completed" while the header printed the raw "done").
// Unknown statuses fall through to the raw token.  (v1.1.11 F-A9)
const _STATUS_LABELS = {
  starting: 'Starting…',
  running:  'Running…',
  done:     'Completed',
  error:    'Error',
  cancelled:'Stopped',
};
function _statusLabel(status) {
  return _STATUS_LABELS[status] || status;
}
// True while the given mode has a session that has not yet reached a terminal
// state.  Gates the run button (prevents a double-submit spawning a second
// pipeline) and decides whether ■ Stop needs a confirm.  (v1.1.11 F-A1)
function _modeHasLiveSession(mode) {
  return (State.sessions[mode] || []).some(
    s => s.status === 'starting' || s.status === 'running'
  );
}

// Recompute the global topbar run-status pill from ALL sessions across both
// modes, so a running pipeline is never invisible from Dashboard/Settings.
// Guarded no-op if the pill node is absent.  (v1.1.11 F-B7)
function _updateGlobalRunPill() {
  const pill = document.getElementById('global-run-pill');
  if (!pill) return;
  let running = 0;
  ['project', 'idea'].forEach(mode => {
    (State.sessions[mode] || []).forEach(s => {
      if (s.status === 'running' || s.status === 'starting') running++;
    });
  });
  if (running > 0) {
    const word = (typeof CURRENT_LANG !== 'undefined' && CURRENT_LANG === 'zh')
      ? (running === 1 ? '執行中' : `${running} 個執行中`)
      : (running === 1 ? 'Running' : `${running} running`);
    pill.innerHTML = `<span class="run-pill-dot"></span>${word}`;
    pill.hidden = false;
  } else {
    pill.hidden = true;
    pill.textContent = '';
  }
}

function _newSession(mode, analysisType) {
  _sessCounter[mode]++;
  const typeName = { 1:'Quant', 2:'SaaS', 3:'Agent', 4:'Scientist' }[analysisType] || '';
  const label = `${typeName} #${_sessCounter[mode]}`;
  const graphDef = AGENT_GRAPHS[analysisType] || AGENT_GRAPHS[1];
  const agentStates = {};
  graphDef.nodes.forEach(n => { agentStates[n.id] = 'waiting'; });
  return {
    id: `sess_${mode}_${Date.now()}_${_sessCounter[mode]}`,
    run_id: null, mode, label,
    status: 'starting', analysisType,
    lines: [],
    agentStates,
    startedAt: Date.now(), endedAt: null, returncode: null,
  };
}

function _getSession(sessId) {
  for (const mode of ['project','idea']) {
    const s = State.sessions[mode].find(s => s.id === sessId);
    if (s) return s;
  }
  return null;
}

function _activeSession(mode) {
  const id = State.activeSession[mode];
  return id ? State.sessions[mode].find(s => s.id === id) || null : null;
}

function _setActiveSession(mode, sessId) {
  State.activeSession[mode] = sessId;
  _renderSessionBar(mode);
  _rebuildTerminal(mode);
  _refreshAgentFlow(mode);
  _updateSessionHeader(mode);
}

function _renderSessionBar(mode) {
  const bar = document.getElementById(`session-bar-${mode}`);
  if (!bar) return;
  const sessions = State.sessions[mode];
  if (!sessions.length) { bar.innerHTML = ''; return; }
  bar.innerHTML = sessions.map(s => {
    const active = s.id === State.activeSession[mode] ? ' s-active' : '';
    const statusCls = ` s-${s.status}`;
    // Use data attributes so that special characters (including ') in mode/sessId
    // are never interpreted by the JS engine inside a string literal — the HTML
    // parser would decode &#39; back to ' before the JS engine sees it, breaking
    // single-quoted string literals even after escHtml() encoding.
    return `<div class="session-tab${active}${statusCls}" data-mode="${escHtml(mode)}" data-sess-id="${escHtml(s.id)}" onclick="_setActiveSession(this.dataset.mode, this.dataset.sessId)">
      <span class="sess-dot"></span>
      <span>${escHtml(s.label)}</span>
      <button class="sess-close" data-mode="${escHtml(mode)}" data-sess-id="${escHtml(s.id)}" onclick="event.stopPropagation();_removeSession(this.dataset.mode, this.dataset.sessId)" title="Close">×</button>
    </div>`;
  }).join('');
}

function _removeSession(mode, sessId) {
  const sess = _getSession(sessId);
  if (sess && (sess.status === 'running' || sess.status === 'starting')) {
    // v1.1.11 (F-A10): closing a tab whose pipeline is still running aborts it
    // — confirm first.
    if (!confirm('Close this session? Its run is still active and will be stopped.')) {
      return;
    }
    _closeEvtSource(sessId);
    if (sess.run_id) fetch(`/api/run/${sess.run_id}`, { method: 'DELETE' }).catch(() => {});
  }
  _stopElapsedTimer(sessId);
  State.sessions[mode] = State.sessions[mode].filter(s => s.id !== sessId);
  if (State.activeSession[mode] === sessId) {
    const remaining = State.sessions[mode];
    State.activeSession[mode] = remaining.length ? remaining[remaining.length - 1].id : null;
  }
  _renderSessionBar(mode);
  _rebuildTerminal(mode);
  _refreshAgentFlow(mode);
  _updateSessionHeader(mode);
  if (typeof _updateGlobalRunPill === 'function') _updateGlobalRunPill();  // F-B7
}

function _updateSessionHeader(mode) {
  const sess = _activeSession(mode);
  const labelEl = document.getElementById(`term-label-${mode}`);
  const metaEl  = document.getElementById(`term-meta-${mode}`);
  if (!labelEl) return;
  if (!sess) {
    labelEl.textContent = 'No session';
    if (metaEl) metaEl.textContent = '';
    return;
  }
  labelEl.textContent = sess.label;
  if (metaEl) {
    const elapsed = sess.endedAt
      ? _fmtElapsed(sess.endedAt - sess.startedAt)
      : _fmtElapsed(Date.now() - sess.startedAt);
    metaEl.textContent = `${_statusLabel(sess.status)}  ·  ${elapsed}`;
  }
}

function _fmtElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  return `${Math.floor(m/60)}h ${m%60}m`;
}

function _startElapsedTimer(sessId) {
  _stopElapsedTimer(sessId);
  const sess = _getSession(sessId);
  if (!sess) return;
  State._elapsedTimers[sessId] = setInterval(() => {
    if (sessId !== State.activeSession[sess.mode]) return;
    _updateSessionHeader(sess.mode);
  }, 1000);
}

function _stopElapsedTimer(sessId) {
  if (State._elapsedTimers[sessId]) {
    clearInterval(State._elapsedTimers[sessId]);
    delete State._elapsedTimers[sessId];
  }
}

// ─── Terminal helpers ─────────────────────────────────────────
function _rebuildTerminal(mode) {
  const t = document.getElementById(`terminal-${mode}`);
  if (!t) return;
  const sess = _activeSession(mode);
  if (!sess) { t.innerHTML = '<span class="term-cursor"></span>'; return; }
  const frag = document.createDocumentFragment();
  sess.lines.forEach(({ text, cls }) => {
    const div = document.createElement('div');
    div.className = 'term-line' + (cls ? ' ' + cls : '');
    div.textContent = text;
    frag.appendChild(div);
  });
  const cursor = document.createElement('span');
  cursor.className = 'term-cursor';
  frag.appendChild(cursor);
  t.innerHTML = '';
  t.appendChild(frag);
  t.scrollTop = t.scrollHeight;
}

// Maximum characters per terminal line and maximum lines kept in history.
const _MAX_LINE_CHARS = 10_000;
const _MAX_TERM_LINES = 5_000;

function _appendLine(sessId, text, cls = '') {
  const sess = _getSession(sessId);
  if (!sess) return;
  // Truncate runaway lines before storing to prevent DOM/memory bloat.
  if (text.length > _MAX_LINE_CHARS) text = text.slice(0, _MAX_LINE_CHARS) + '…';
  sess.lines.push({ text, cls });
  // Trim oldest lines when history grows beyond the cap.
  if (sess.lines.length > _MAX_TERM_LINES) sess.lines = sess.lines.slice(-_MAX_TERM_LINES);
  if (sessId !== State.activeSession[sess.mode]) return;
  const t = document.getElementById(`terminal-${sess.mode}`);
  if (!t) return;
  const cursor = t.querySelector('.term-cursor');
  const div = document.createElement('div');
  div.className = 'term-line' + (cls ? ' ' + cls : '');
  div.textContent = text;
  t.insertBefore(div, cursor);
  // Symmetric DOM trim: cap the on-screen `.term-line` node count to
  // mirror the in-memory sess.lines cap above.  Without this the active
  // terminal accumulates one <div> per streamed line forever — multi-hour
  // pipelines / overnight backtests grow the DOM into the tens of
  // thousands of children, layout/paint slows to a crawl, and Chrome's
  // Memory Saver eventually discards the tab so the user comes back to
  // a frozen page.  The cap is enforced by removing the OLDEST line
  // (firstElementChild) until the count matches `_MAX_TERM_LINES`; the
  // trailing `.term-cursor` span is preserved so the next insertBefore
  // still has its anchor.
  const cursorPresent = cursor != null;
  let lineCount = t.childElementCount - (cursorPresent ? 1 : 0);
  while (lineCount > _MAX_TERM_LINES) {
    const first = t.firstElementChild;
    if (!first || first === cursor) break;
    t.removeChild(first);
    lineCount--;
  }
  t.scrollTop = t.scrollHeight;
}

function _classifyLine(line) {
  if (/\berror\b|exception|traceback|❌/i.test(line)) return 'err';
  if (/\bwarn(ing)?\b|⚠️/i.test(line)) return 'warn';
  if (/✅|completed successfully|run finished/i.test(line)) return 'success';
  if (/^={3,}|^-{3,}|\[Stage\s|\bStage\s+\d/i.test(line)) return 'stage';
  if (/^\$\s|^\[INFO\]|^\[Debug\]/i.test(line)) return 'dim';
  return '';
}

function clearActiveSession(mode) {
  const sess = _activeSession(mode);
  if (!sess) return;
  // v1.1.11 (F-A10): confirm only when there is terminal output to lose.
  if (sess.lines && sess.lines.length &&
      !confirm('Clear the terminal output for this session? This cannot be undone.')) {
    return;
  }
  sess.lines = [];
  _rebuildTerminal(mode);
}

function _setSessionStatus(sessId, status, returncode = null) {
  const sess = _getSession(sessId);
  if (!sess) return;
  sess.status = status;
  if (returncode !== null) sess.returncode = returncode;
  if (status === 'done' || status === 'error' || status === 'cancelled') {
    sess.endedAt = Date.now();
    _stopElapsedTimer(sessId);
    _updateSessionHeader(sess.mode);
    // Flush any agent node that is still 'active' at run-end so the flow diagram
    // does not freeze with spinning nodes.  Nodes that were never activated
    // ('waiting') are intentionally left as-is — they genuinely did not run.
    if (sess.agentStates) {
      let _flowChanged = false;
      Object.keys(sess.agentStates).forEach(k => {
        if (sess.agentStates[k] === 'active') {
          sess.agentStates[k] = 'done';
          _flowChanged = true;
        }
      });
      if (_flowChanged &&
          sessId === State.activeSession[sess.mode] &&
          State.activeView[sess.mode] === 'agentflow') {
        _refreshAgentFlow(sess.mode);
      }
    }
  }
  _renderSessionBar(sess.mode);
  const el = document.getElementById(`run-status-${sess.mode}`);
  const inlineEl = document.getElementById(`run-status-${sess.mode}-inline`);
  const pill = `<span class="status-pill status-${status}"><span class="pulse"></span>${_statusLabel(status)}</span>`;
  if (el) el.innerHTML = pill;
  if (inlineEl) inlineEl.innerHTML = pill;
  // v1.1.11 (F-A1): single chokepoint for the per-mode run button — it stays
  // disabled while this mode still has a starting/running session and
  // re-enables only when every session in the mode is terminal.  Prevents the
  // double-submit the old "re-enable right after the POST" path allowed.
  const _runBtn = document.getElementById(`btn-run-${sess.mode}`);
  if (_runBtn) _runBtn.disabled = _modeHasLiveSession(sess.mode);
  // v1.1.11 (F-B7): keep the global topbar run pill in sync.
  if (typeof _updateGlobalRunPill === 'function') _updateGlobalRunPill();
}

// ─── Agent flow ───────────────────────────────────────────────
function _detectAgentActivity(sessId, line) {
  const sess = _getSession(sessId);
  if (!sess) return;
  let changed = false;
  const graphDef = AGENT_GRAPHS[sess.analysisType] || AGENT_GRAPHS[1];

  function _inferPriorDone(nodeId) {
    const node = graphDef.nodes.find(n => n.id === nodeId);
    if (!node) return;
    graphDef.nodes.forEach(n => {
      // Only mark 'active' nodes as done — nodes still 'waiting' have
      // never run and must NOT be shown as completed.
      if (n.stage < node.stage && sess.agentStates[n.id] === 'active') {
        sess.agentStates[n.id] = 'done';
        changed = true;
      }
    });
  }

  // CrewAI's Printer emits `# Agent: <role>` (see crewai/utilities/agent_utils.py
  // show_agent_logs) wrapped in ANSI color codes; older CrewAI versions and some
  // internal log lines use `Working Agent: <role>`.  Accept both so every crew-
  // level agent transition (research_synthesizer, analysts, judges, ...) lights
  // up the flow diagram, not just lines that happen to contain "Working Agent".
  const workMatch = line.match(/(?:Working Agent|#\s*Agent)[:\s]+(.+)/i);
  if (workMatch) {
    // Strip ANSI escape sequences (\x1b[...m) so AGENT_PATTERNS like
    // /^\s*Librarian\s*$/ keep matching when CrewAI wraps the role in color codes.
    const name = workMatch[1].replace(/\x1b\[[0-9;]*m/g, '').trim();
    for (const { re, id } of AGENT_PATTERNS) {
      if (re.test(name)) {
        Object.keys(sess.agentStates).forEach(k => {
          if (sess.agentStates[k] === 'active') { sess.agentStates[k] = 'done'; changed = true; }
        });
        _inferPriorDone(id);
        sess.agentStates[id] = 'active'; changed = true; break;
      }
    }
  }

  const evMap = [
    [/direction_seed_kickoff_start/,                     'dir_seed',    'active'],
    [/direction_seed_kickoff_(done|completed|success)/i, 'dir_seed',    'done'  ],
    [/direction_seed_kickoff_failed|direction_seed.*fail/i,'dir_seed',  'error' ],
    // librarian_phase_start: activate librarian + all stage-2 research lane nodes
    [/event=.*librarian.*start|librarian.*kickoff_start/i, null,        'librarian_phase_start'],
    // Per-task transitions for the research swarm crew.  The crew runs with
    // verbose=False (so CrewAI does not print the `# Agent:` header per task)
    // — instead the crew's task_callback emits a `research_lane_done` log
    // event after every task.  Those structured events let the synthesizer
    // node light up while it actually runs, instead of only flashing for
    // ~650ms when librarian_kickoff_done fires (research_phase_done fallback).
    // ORDER MATTERS: the more specific `research_synthesizer` pattern must
    // appear BEFORE patterns that could accidentally match it (none here,
    // but be careful when adding new entries).
    [/research_lane_done.*market_research/i,              'mkt_res',     'done'                  ],
    [/research_lane_done.*technical_research/i,           'tech_res',    'done'                  ],
    [/research_lane_done.*competitor_research/i,          null,          'synthesizer_starts'    ],
    [/research_lane_done.*research_synthesizer/i,         'synthesizer', 'done'                  ],
    // research_phase_done: mark all stage 1-3 nodes done (librarian + lanes + synthesizer)
    [/librarian_kickoff_done|event=.*librarian.*done/i,    null,        'research_phase_done'  ],
    [/event=.*gate.*start|gate_controller.*start/i,       'gate',       'active'],
    [/event=.*gate.*done|gate_controller.*done/i,         'gate',       'done'  ],
    [/event=.*self_check.*start/i,                        'self_check', 'active'],
    [/event=.*self_check.*done/i,                         'self_check', 'done'  ],
    [/Starting direction seed/i,                          'dir_seed',   'active'],
    [/Direction seed plan/i,                              'dir_seed',   'done'  ],
    [/stage=direction_seed/i,                             'dir_seed',   'active'],
    [/direction_debate_kickoff_start/i,                   'dir_judge',  'active'      ],
    [/direction_debate_kickoff_(done|completed|success)/i,'dir_judge',  'done'        ],
    [/direction_debate_kickoff_failed|direction_debate.*fail/i,'dir_judge','error'    ],
    [/analysis_kickoff_start/i,                           null,         'stage5_active'],
    // analysis_phase_done: mark stages 5-7 done (analysts + assembler + gate controller)
    [/analysis_kickoff_done/i,                            null,         'analysis_phase_done' ],
    // v1.0.5 frontend↔backend audit: backend emits ``analysis_kickoff_failed``
    // when the analysis crew raises during kickoff.  Without this mapping
    // the analysts node would stay ``active`` indefinitely after a crash.
    [/analysis_kickoff_failed|analysis_kickoff.*fail/i,   null,         'analysis_phase_error'],
    // librarian_kickoff_failed: backend section_02 emits this when the
    // research crew kickoff raises.  Map it to the librarian node so the
    // graph turns red instead of staying green-active for the rest of run.
    [/librarian_kickoff_failed|librarian_kickoff.*fail/i, 'librarian',  'error'       ],
    [/codegen_kickoff_start/i,                            null,         'stage8_active'],
    // codegen_kickoff_done dispatches to the ``codegen_phase_done`` state
    // handler (below): it first marks every stage-8 node ``done``, then
    // activates self_check so stage 9 visibly takes over.  Mirrors the
    // analysis_phase_done / research_phase_done pattern for the codegen
    // lane (without this, stage-8 nodes would be stuck showing 'active'
    // for the rest of the run because nothing else closed them).
    [/codegen_kickoff_done/i,                             null,         'codegen_phase_done'],
    [/codegen_kickoff_failed/i,                           'code_gen',   'error'       ],
    // v1.0.5 frontend↔backend audit: project_fix is the quality loop's
    // re-codegen phase, fired by section_07 when self_check finds issues
    // and the loop has retry budget left.  Backend emits 3 structured
    // events: project_fix_kickoff_{start,done,failed}.  Without these
    // mappings the agent flow goes silent for the full duration of the
    // quality loop (often 30-60s × N rounds — the entire v1.0.5 round
    // 2/3 work area).  Light up code_gen during the fix so the operator
    // sees re-codegen is in progress; dispatch ``codegen_phase_done`` on
    // completion so self_check re-activates and the loop visibly cycles.
    [/project_fix_kickoff_start/i,                        'code_gen',   'active'      ],
    [/project_fix_kickoff_done/i,                         null,         'codegen_phase_done'],
    [/project_fix_kickoff_failed|project_fix_kickoff.*fail/i, 'code_gen', 'error'      ],
    // direction_feedback_start fires when GateController requests a
    // direction-debate refinement after analysis.  Activating dir_judge
    // (stage 0 / debate node) for the duration of the rerun gives the
    // user a visible signal that the feedback loop is in progress;
    // without this mapping, the graph would appear frozen for 30-60s
    // while the debate ran.
    [/direction_feedback_start/i,                         'dir_judge',  'active'      ],
    [/direction_feedback_failed/i,                        'dir_judge',  'error'       ],
    [/stage=gate\b/i,                                     'gate',       'active'      ],
    [/stage=self_check\b/i,                               'self_check', 'active'      ],
  ];
  for (const [re, id, state] of evMap) {
    if (re.test(line)) {
      if (state === 'done_all') {
        Object.keys(sess.agentStates).forEach(k => {
          if (sess.agentStates[k] === 'active') {
            sess.agentStates[k] = 'done';
          }
        });
        changed = true;
      } else if (state === 'librarian_phase_start') {
        // Activate librarian (stage 1) + all stage-2 research lane nodes in one shot.
        // _inferPriorDone ensures dir_seed (stage 0) is closed if still active.
        _inferPriorDone('librarian');
        graphDef.nodes.filter(n => n.stage === 1 || n.stage === 2).forEach(n => {
          if (sess.agentStates[n.id] !== undefined) {
            sess.agentStates[n.id] = 'active'; changed = true;
          }
        });
      } else if (state === 'synthesizer_starts') {
        // Triggered by `research_lane_done lane=competitor_research` (the third
        // and last lane in the research swarm).  Close any stage-1/2 nodes that
        // are still `active` (librarian + market_research + technical_research
        // + competitor_research itself) and light up the synthesizer (stage 3)
        // so it shows as in-progress for the duration of its actual task run,
        // instead of only flashing for ~650ms when librarian_kickoff_done fires.
        graphDef.nodes.filter(n => n.stage >= 1 && n.stage <= 2).forEach(n => {
          if (sess.agentStates[n.id] === 'active' || sess.agentStates[n.id] === 'waiting') {
            sess.agentStates[n.id] = 'done'; changed = true;
          }
        });
        if (sess.agentStates['synthesizer'] !== undefined) {
          sess.agentStates['synthesizer'] = 'active'; changed = true;
        }
      } else if (state === 'research_phase_done') {
        // Close all research-phase nodes (stages 1-3): librarian + lanes + synthesizer.
        // For nodes still 'waiting' (most commonly synthesizer when its individual
        // `# Agent:` log line wasn't captured), flash 'active' now and defer 'done'
        // to a later macrotask so the user actually sees the highlight paint.
        // Setting 'active' and 'done' in the same synchronous tick would never
        // render the intermediate state.
        const _missedActive = [];
        graphDef.nodes.filter(n => n.stage >= 1 && n.stage <= 3).forEach(n => {
          if (sess.agentStates[n.id] !== undefined) {
            if (sess.agentStates[n.id] === 'waiting') {
              sess.agentStates[n.id] = 'active'; // visible flash
              _missedActive.push(n.id);
              changed = true;
            } else {
              sess.agentStates[n.id] = 'done'; changed = true;
            }
          }
        });
        if (_missedActive.length) {
          const _sessIdSnap = sessId, _modeSnap = sess.mode;
          setTimeout(() => {
            const _s = _getSession(_sessIdSnap);
            if (!_s) return;
            let _changed = false;
            _missedActive.forEach(nid => {
              if (_s.agentStates[nid] === 'active') {
                _s.agentStates[nid] = 'done'; _changed = true;
              }
            });
            if (_changed &&
                _sessIdSnap === State.activeSession[_modeSnap] &&
                State.activeView[_modeSnap] === 'agentflow') {
              _refreshAgentFlow(_modeSnap);
            }
          }, 650);
        }
      } else if (state === 'analysis_phase_done') {
        // The analysis crew runs analysts (stage 5), assembler (stage 6), and gate
        // controller (stage 7) sequentially.  Mark them done — any node still
        // 'waiting' flashes 'active' first (via deferred 'done') so it visibly lights up.
        const _s5first = graphDef.nodes.find(n => n.stage === 5);
        if (_s5first) _inferPriorDone(_s5first.id);
        const _missedActive2 = [];
        graphDef.nodes.filter(n => n.stage >= 5 && n.stage <= 7).forEach(n => {
          if (sess.agentStates[n.id] !== undefined) {
            if (sess.agentStates[n.id] === 'waiting') {
              sess.agentStates[n.id] = 'active';
              _missedActive2.push(n.id);
              changed = true;
            } else {
              sess.agentStates[n.id] = 'done'; changed = true;
            }
          }
        });
        if (_missedActive2.length) {
          const _sessIdSnap2 = sessId, _modeSnap2 = sess.mode;
          setTimeout(() => {
            const _s = _getSession(_sessIdSnap2);
            if (!_s) return;
            let _changed = false;
            _missedActive2.forEach(nid => {
              if (_s.agentStates[nid] === 'active') {
                _s.agentStates[nid] = 'done'; _changed = true;
              }
            });
            if (_changed &&
                _sessIdSnap2 === State.activeSession[_modeSnap2] &&
                State.activeView[_modeSnap2] === 'agentflow') {
              _refreshAgentFlow(_modeSnap2);
            }
          }, 650);
        }
      } else if (state === 'stage5_active') {
        Object.keys(sess.agentStates).forEach(k => {
          if (sess.agentStates[k] === 'active') { sess.agentStates[k] = 'done'; }
        });
        const _s5 = graphDef.nodes.filter(n => n.stage === 5);
        if (_s5.length) _inferPriorDone(_s5[0].id);
        _s5.forEach(n => { sess.agentStates[n.id] = 'active'; changed = true; });
        // Assembler (stage 6) and gate controller (stage 7) run sequentially
        // after the analysts — activate them so they show as in-progress.
        if (sess.agentStates['assembler'] !== undefined) {
          sess.agentStates['assembler'] = 'active'; changed = true;
        }
        if (sess.agentStates['gate'] !== undefined) {
          sess.agentStates['gate'] = 'active'; changed = true;
        }
      } else if (state === 'stage8_active') {
        Object.keys(sess.agentStates).forEach(k => {
          if (sess.agentStates[k] === 'active') { sess.agentStates[k] = 'done'; }
        });
        const _s8 = graphDef.nodes.filter(n => n.stage === 8);
        if (_s8.length) _inferPriorDone(_s8[0].id);
        _s8.forEach(n => { sess.agentStates[n.id] = 'active'; changed = true; });
      } else if (state === 'analysis_phase_error') {
        // v1.0.5 audit: ``analysis_kickoff_failed`` fires when the
        // analysis crew (analysts + assembler + gate_controller) crashes
        // during kickoff.  Mark every stage-5/6/7 node currently active or
        // waiting as ``error`` so the operator sees a red dot exactly
        // where the crash happened, not a stuck green-active node.  We
        // never auto-promote ``done`` nodes back to ``error`` — once a
        // node is closed cleanly its history is preserved.
        graphDef.nodes.filter(n => n.stage >= 5 && n.stage <= 7).forEach(n => {
          if (sess.agentStates[n.id] !== undefined &&
              sess.agentStates[n.id] !== 'done') {
            sess.agentStates[n.id] = 'error'; changed = true;
          }
        });
      } else if (state === 'codegen_phase_done') {
        // Close all stage-8 nodes (code_arch, code_gen, …) and activate
        // self_check so stage 9 visibly takes over.  Without the explicit
        // close, code_gen would stay 'active' for the rest of the run
        // because no other event ever closes it.  Any stage-8
        // node still 'waiting' (rare — would mean the codegen kickoff
        // started without _inferPriorDone running) flashes 'active'
        // briefly via the deferred-done pattern used by
        // research_phase_done so the highlight visibly paints.
        const _missedS8 = [];
        graphDef.nodes.filter(n => n.stage === 8).forEach(n => {
          if (sess.agentStates[n.id] !== undefined) {
            if (sess.agentStates[n.id] === 'waiting') {
              sess.agentStates[n.id] = 'active';
              _missedS8.push(n.id);
              changed = true;
            } else {
              sess.agentStates[n.id] = 'done'; changed = true;
            }
          }
        });
        if (sess.agentStates['self_check'] !== undefined) {
          sess.agentStates['self_check'] = 'active'; changed = true;
        }
        if (_missedS8.length) {
          const _sessIdSnap8 = sessId, _modeSnap8 = sess.mode;
          setTimeout(() => {
            const _s = _getSession(_sessIdSnap8);
            if (!_s) return;
            let _changed = false;
            _missedS8.forEach(nid => {
              if (_s.agentStates[nid] === 'active') {
                _s.agentStates[nid] = 'done'; _changed = true;
              }
            });
            if (_changed &&
                _sessIdSnap8 === State.activeSession[_modeSnap8] &&
                State.activeView[_modeSnap8] === 'agentflow') {
              _refreshAgentFlow(_modeSnap8);
            }
          }, 650);
        }
      } else if (id) {
        if (state === 'active') {
          Object.keys(sess.agentStates).forEach(k => {
            if (sess.agentStates[k] === 'active') { sess.agentStates[k] = 'done'; }
          });
          _inferPriorDone(id);
        } else if (state === 'done') {
          _inferPriorDone(id);
        }
        sess.agentStates[id] = state;
        changed = true;
      }
      break;
    }
  }

  if (!changed) {
    for (const { re, id } of AGENT_PATTERNS) {
      if (re.test(line)) {
        if (/(kickoff|assigned|Working Agent)/i.test(line)) {
          Object.keys(sess.agentStates).forEach(k => { if (sess.agentStates[k]==='active') sess.agentStates[k]='done'; });
          _inferPriorDone(id);
          sess.agentStates[id] = 'active'; changed = true;
        } else if (/(done|finish|complet|Final Answer)/i.test(line) && sess.agentStates[id]==='active') {
          _inferPriorDone(id);
          sess.agentStates[id] = 'done'; changed = true;
        } else if (/(error|fail|exception)/i.test(line) && sess.agentStates[id]==='active') {
          sess.agentStates[id] = 'error'; changed = true;
        }
        if (changed) break;
      }
    }
  }

  if (changed && sessId === State.activeSession[sess.mode] && State.activeView[sess.mode] === 'agentflow') {
    _refreshAgentFlow(sess.mode);
  }
}

function _refreshAgentFlow(mode) {
  const wrap = document.getElementById(`agentflow-svg-wrap-${mode}`);
  if (!wrap) return;
  const sess = _activeSession(mode);
  if (!sess) {
    wrap.innerHTML = '<div class="af-empty">Start a run to see the agent flow</div>';
    _renderStageStats(mode, null);
    return;
  }
  const graphDef = AGENT_GRAPHS[sess.analysisType] || AGENT_GRAPHS[1];
  _drawAgentFlow(wrap, graphDef, sess.agentStates);
  _renderStageStats(mode, sess);
}

function _drawAgentFlow(container, graphDef, agentStates) {
  const NS = 'http://www.w3.org/2000/svg';
  const S=_AF_S, R=_AF_R, NW=_AF_NW, NH=_AF_NH, PX=_AF_PX, PY=_AF_PY;
  const maxStage = Math.max(...graphDef.nodes.map(n=>n.stage));
  const maxRow   = Math.max(...graphDef.nodes.map(n=>n.row));
  const W = PX + maxStage*S + NW + PX;
  const H = PY + maxRow*R  + NH + PY + 10;

  const svg = document.createElementNS(NS,'svg');
  svg.setAttribute('width', W); svg.setAttribute('height', H);
  svg.style.display = 'block';

  const defs = document.createElementNS(NS,'defs');
  const mkId = 'af-arrow-' + Math.random().toString(36).slice(2,7);
  const marker = document.createElementNS(NS,'marker');
  marker.setAttribute('id', mkId); marker.setAttribute('markerWidth','8');
  marker.setAttribute('markerHeight','8'); marker.setAttribute('refX','7');
  marker.setAttribute('refY','3'); marker.setAttribute('orient','auto');
  const arrowPath = document.createElementNS(NS,'path');
  arrowPath.setAttribute('d','M0,0 L0,6 L8,3 z');
  arrowPath.setAttribute('fill','#2a4070');
  marker.appendChild(arrowPath); defs.appendChild(marker); svg.appendChild(defs);

  const pos = {};
  graphDef.nodes.forEach(n => {
    pos[n.id] = { x: PX + n.stage*S, y: PY + n.row*R };
  });

  graphDef.edges.forEach(([src,dst]) => {
    const s = pos[src], d = pos[dst]; if (!s||!d) return;
    const x1=s.x+NW, y1=s.y+NH/2, x2=d.x, y2=d.y+NH/2;
    const cx=(x1+x2)/2;
    const srcDone = (agentStates[src]==='done');
    const edge = document.createElementNS(NS,'path');
    edge.setAttribute('d',`M${x1},${y1} C${cx},${y1} ${cx},${y2} ${x2},${y2}`);
    edge.setAttribute('stroke', srcDone ? '#2a4a70' : '#1c2e50');
    edge.setAttribute('stroke-width','1.5');
    edge.setAttribute('fill','none');
    edge.setAttribute('marker-end',`url(#${mkId})`);
    svg.appendChild(edge);
  });

  graphDef.nodes.forEach(n => {
    const p = pos[n.id];
    const state = agentStates[n.id] || 'waiting';
    const colors = {
      waiting: { fill:'#0f1830', stroke:'#1c2e50', text:'#3d5278', fw:'400' },
      active:  { fill:'rgba(99,102,241,.22)', stroke:'#6366f1', text:'#a5b4fc', fw:'600' },
      done:    { fill:'rgba(34,211,160,.1)',  stroke:'#22d3a0', text:'#22d3a0', fw:'500' },
      error:   { fill:'rgba(248,113,113,.15)',stroke:'#f87171', text:'#f87171', fw:'500' },
    };
    const c = colors[state] || colors.waiting;

    const g = document.createElementNS(NS,'g');

    if (state === 'active') {
      const glow = document.createElementNS(NS,'rect');
      glow.setAttribute('x',p.x-3); glow.setAttribute('y',p.y-3);
      glow.setAttribute('width',NW+6); glow.setAttribute('height',NH+6);
      glow.setAttribute('rx','10'); glow.setAttribute('fill','none');
      glow.setAttribute('stroke','rgba(99,102,241,.35)');
      glow.setAttribute('stroke-width','2');
      g.appendChild(glow);
    }

    const rect = document.createElementNS(NS,'rect');
    rect.setAttribute('x',p.x); rect.setAttribute('y',p.y);
    rect.setAttribute('width',NW); rect.setAttribute('height',NH);
    rect.setAttribute('rx','7');
    rect.setAttribute('fill',c.fill); rect.setAttribute('stroke',c.stroke);
    rect.setAttribute('stroke-width','1.5');
    g.appendChild(rect);

    const iconMap = { done:'✓', error:'✗', active:'●' };
    if (iconMap[state]) {
      const icon = document.createElementNS(NS,'text');
      icon.setAttribute('x',p.x+NW-8); icon.setAttribute('y',p.y+12);
      icon.setAttribute('text-anchor','middle'); icon.setAttribute('font-size','9');
      icon.setAttribute('fill',c.stroke);
      icon.textContent = iconMap[state];
      g.appendChild(icon);
    }

    const lines = n.label.split('\n');
    const lineH = 13, totalH = lines.length * lineH;
    lines.forEach((ln, i) => {
      const t = document.createElementNS(NS,'text');
      t.setAttribute('x', p.x+NW/2);
      t.setAttribute('y', p.y + NH/2 - totalH/2 + lineH/2 + i*lineH + 1);
      t.setAttribute('text-anchor','middle'); t.setAttribute('dominant-baseline','middle');
      t.setAttribute('font-family',"'Inter',sans-serif"); t.setAttribute('font-size','10');
      t.setAttribute('fill',c.text); t.setAttribute('font-weight',c.fw);
      t.textContent = ln;
      g.appendChild(t);
    });

    svg.appendChild(g);
  });

  container.innerHTML = '';
  container.appendChild(svg);
}

// ─── View switcher ────────────────────────────────────────────
function switchView(mode, view, btn) {
  State.activeView[mode] = view;
  // tab highlights
  const _vtT = document.getElementById(`vtab-terminal-${mode}`);
  const _vtA = document.getElementById(`vtab-agentflow-${mode}`);
  const _vtI = document.getElementById(`vtab-insights-${mode}`);
  if (_vtT) _vtT.classList.toggle('vt-active', view==='terminal');
  if (_vtA) _vtA.classList.toggle('vt-active', view==='agentflow');
  if (_vtI) _vtI.classList.toggle('vt-active', view==='insights');
  // panel visibility
  const tw = document.getElementById(`terminal-wrap-${mode}`);
  const ap = document.getElementById(`agentflow-panel-${mode}`);
  const ip = document.getElementById(`insights-panel-${mode}`);
  if (tw) tw.style.display = (view === 'terminal') ? '' : 'none';
  if (ap) ap.classList.toggle('af-visible', view === 'agentflow');
  if (ip) ip.classList.toggle('insights-visible', view === 'insights');
  if (view === 'agentflow') {
    _refreshAgentFlow(mode);
  } else if (view === 'insights') {
    _refreshInsightsPanel(mode);
  }
}

// ─── Run Insights view ────────────────────────────────────────
// v1.1.0: fetch and render the per-run insights ledger for the active
// session.  The panel reads ``State.activeSession[mode]`` to find the
// run_id, then hits ``GET /api/run/<run_id>/insights``.
async function _refreshInsightsPanel(mode) {
  const sessId = State.activeSession && State.activeSession[mode];
  const body = document.getElementById(`insights-body-${mode}`);
  const meta = document.getElementById(`insights-meta-${mode}`);
  if (!body) return;
  if (!sessId) {
    if (meta) meta.textContent = 'no session selected';
    body.innerHTML = '<div class="af-empty">Start a run, or pick a session from the bar above.</div>';
    return;
  }
  const sess = _getSession(sessId);
  if (!sess || !sess.run_id) {
    if (meta) meta.textContent = 'session has no run_id yet';
    body.innerHTML = '<div class="af-empty">Waiting for the run to register a run_id…</div>';
    return;
  }
  if (meta) meta.textContent = `run_id=${sess.run_id}`;
  body.innerHTML = '<div class="af-empty">Loading insights…</div>';
  try {
    const resp = await fetch(`/api/run/${encodeURIComponent(sess.run_id)}/insights`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    body.innerHTML = _renderInsightsForRun(data);
  } catch (err) {
    // v1.1.0 fifth-pass (G-23): use the centralised _escapeHtml helper
    // so quotes and apostrophes are escaped consistently with the
    // other six error-rendering paths in this file.  The prior
    // strip-only pass missed `"` and `'`, which would have allowed
    // attribute-context injection if a future refactor moved this
    // node into an attribute slot.
    body.innerHTML = `<div class="af-empty">Failed to load insights: ${_escapeHtml(String(err))}</div>`;
  }
}

function _escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function _renderInsightsForRun(data) {
  const total = (data && data.total) || 0;
  if (!total) {
    return '<div class="af-empty">No insight events recorded for this run yet. Run insights are written when Stage 0 force-nones, retries exhaust, or the project is saved.</div>';
  }
  const streamLabels = {
    output: 'Output Method',
    error:  'Error Records',
    debate: 'Direction Debate Rejections',
    params: 'Runtime Parameters',
  };
  const streamIcons = {
    output: '✅',
    error:  '⚠️',
    debate: '⛔',
    params: '⚙️',
  };
  const parts = [`<div class="insights-summary">Total events: <strong>${total}</strong></div>`];
  ['debate', 'error', 'output', 'params'].forEach((s) => {
    const events = (data.streams && data.streams[s]) || [];
    if (!events.length) return;
    parts.push(
      `<div class="insights-stream-block">
         <div class="insights-stream-header">
           <span class="insights-stream-icon">${streamIcons[s] || '•'}</span>
           <span class="insights-stream-title">${streamLabels[s] || s}</span>
           <span class="insights-stream-count">${events.length}</span>
         </div>
         <div class="insights-stream-events">
           ${events.map(_renderInsightEventRow).join('')}
         </div>
       </div>`
    );
  });
  return parts.join('');
}

function _renderInsightEventRow(ev) {
  const ts = _escapeHtml(ev.ts || '');
  const stage = _escapeHtml(ev.stage || '');
  const kind = _escapeHtml(ev.kind || '');
  const outcome = (ev.outcome && ev.outcome.status) || '';
  const signals = Array.isArray(ev.signals) ? ev.signals : [];
  const sigTags = signals.slice(0, 8).map(s =>
    `<span class="insight-signal-tag">${_escapeHtml(s)}</span>`
  ).join('');
  let detail = '';
  const p = ev.payload || {};
  if (kind === 'direction_debate_rejection') {
    detail = `<div class="insight-detail-line"><strong>reason:</strong> ${_escapeHtml(p.rejection_reason || '')}` +
             (p.judge_verdict_excerpt ? ` &middot; <span class="insight-detail-excerpt">${_escapeHtml(p.judge_verdict_excerpt)}</span>` : '') + '</div>';
  } else if (kind === 'error_record') {
    detail = `<div class="insight-detail-line"><strong>${_escapeHtml(p.exception_class || 'Error')}</strong>` +
             (p.message_head ? ` &middot; <span class="insight-detail-excerpt">${_escapeHtml(p.message_head)}</span>` : '') +
             ` &middot; retries=${_escapeHtml(p.retry_count == null ? '-' : p.retry_count)}</div>`;
  } else if (kind === 'output_method') {
    const parts = [];
    if (p.primary_model_id) parts.push(`model=${_escapeHtml(p.primary_model_id)}`);
    if (p.framework) parts.push(`framework=${_escapeHtml(p.framework)}`);
    if (p.validation_verdict) parts.push(`validation=${_escapeHtml(p.validation_verdict)}`);
    detail = `<div class="insight-detail-line">${parts.join(' · ')}</div>`;
  } else if (kind === 'runtime_params') {
    const flagsCount = p.cli_flags ? Object.keys(p.cli_flags).length : 0;
    detail = `<div class="insight-detail-line">mode=${_escapeHtml(p.mode || '')} · provider=${_escapeHtml(p.llm_provider || '')} · ${flagsCount} flag(s)</div>`;
  }
  const outcomeClass = outcome === 'success' ? 'insight-outcome-success'
    : outcome === 'failure' ? 'insight-outcome-failure'
    : outcome === 'partial' ? 'insight-outcome-partial'
    : 'insight-outcome-neutral';
  return `<div class="insight-event-row">
            <div class="insight-event-meta">
              <span class="insight-event-ts">${ts}</span>
              <span class="insight-event-stage">${stage}</span>
              <span class="insight-event-outcome ${outcomeClass}">${_escapeHtml(outcome)}</span>
            </div>
            ${detail}
            ${sigTags ? `<div class="insight-event-tags">${sigTags}</div>` : ''}
          </div>`;
}

// Dashboard widget: total events per stream + recent global feed.
async function loadInsightsDashboard() {
  const body = document.getElementById('insights-dashboard-body');
  if (!body) return;
  try {
    const resp = await fetch('/api/insights/summary');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    if (!data || !data.enabled) {
      body.innerHTML = '<div class="empty-state"><div class="em-icon">⏸</div>Run Insights subsystem is disabled (CRUCIBLE_RUN_INSIGHTS_ENABLED=0).</div>';
      return;
    }
    const streams = data.streams || {};
    const cells = ['output', 'error', 'debate', 'params'].map(s => {
      const info = streams[s] || {};
      return `<div class="insights-mini-stat">
                <div class="insights-mini-label">${s}.jsonl</div>
                <div class="insights-mini-value">${(info.lines||0).toLocaleString()}</div>
              </div>`;
    }).join('');
    const recent = Array.isArray(data.recent) ? data.recent : [];
    const recentList = recent.length
      ? `<div class="insights-recent-list">${recent.map(_renderInsightEventRow).join('')}</div>`
      : '<div class="af-empty">No events yet. The ledger fills up as runs complete.</div>';
    body.innerHTML = `
      <div class="insights-dashboard-stats">${cells}</div>
      <div class="insights-dashboard-recent">
        <div class="insights-section-title">Recent events</div>
        ${recentList}
      </div>
      <div class="insights-dashboard-footer">
        <small>Ledger root: <code>${_escapeHtml(data.root || '')}</code> · schema v${data.schema_version || 1}</small>
      </div>`;
  } catch (err) {
    body.innerHTML = `<div class="empty-state"><div class="em-icon">⚠</div>Failed to load: ${_escapeHtml(''+err)}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
//  RUN MANAGEMENT
// ═══════════════════════════════════════════════════════════════
function _closeEvtSource(sessId) {
  if (State._evtSources[sessId]) {
    try { State._evtSources[sessId].close(); } catch(_) {}
    delete State._evtSources[sessId];
  }
  if (State._evtTimers[sessId]) {
    clearTimeout(State._evtTimers[sessId]);
    delete State._evtTimers[sessId];
  }
}

// Flag an input as invalid (startRun empty-field validation, v1.1.11 F-A4).
// Sets aria-invalid + an .input-invalid class and an inline outline fallback so
// the signal is visible regardless of CSS, then clears both on the next
// edit/focus so the field does not stay red.
function _markFieldInvalid(el) {
  if (!el) return;
  el.setAttribute('aria-invalid', 'true');
  el.classList.add('input-invalid');
  el.style.outline = '2px solid var(--error, #f87171)';
  const clear = () => {
    el.removeAttribute('aria-invalid');
    el.classList.remove('input-invalid');
    el.style.outline = '';
    el.removeEventListener('input', clear);
    el.removeEventListener('focus', clear);
  };
  el.addEventListener('input', clear);
  el.addEventListener('focus', clear);
  try { el.focus(); } catch (_) {}
}

async function startRun(mode) {
  const flags = collectFlags(mode);
  const analysisType = State.pages[mode].analysisType;
  let payload;
  if (mode === 'project') {
    const pathEl = document.getElementById('project-path');
    const path = (pathEl ? pathEl.value : '').trim();
    if (!path) { _markFieldInvalid(pathEl); showToast('Please enter a project path.', 'warn'); return; }
    payload = { mode:'project', analysis_type:analysisType, project_path:path, flags };
  } else {
    const ideaEl = document.getElementById('idea-text');
    const idea = (ideaEl ? ideaEl.value : '').trim();
    if (!idea) { _markFieldInvalid(ideaEl); showToast('Please enter an idea or strategy description.', 'warn'); return; }
    payload = { mode:'idea', analysis_type:analysisType, idea, flags };
  }

  const sess = _newSession(mode, analysisType);
  while (State.sessions[mode].length >= _MAX_SESSIONS) {
    const oldest = State.sessions[mode].shift();
    _closeEvtSource(oldest.id); _stopElapsedTimer(oldest.id);
  }
  State.sessions[mode].push(sess);
  _setActiveSession(mode, sess.id);

  _setSessionStatus(sess.id, 'starting');
  _startElapsedTimer(sess.id);

  if (State.activeView[mode] !== 'terminal') switchView(mode, 'terminal', null);

  const runBtn = document.getElementById(`btn-run-${mode}`);
  if (runBtn) runBtn.disabled = true;

  try {
    const resp = await fetch('/api/run', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    if (!data.run_id) throw new Error('Server returned no run_id — cannot stream output.');
    sess.run_id = data.run_id;
    // v1.1.11 (F-A1): keep the run button DISABLED while this run streams —
    // re-enabling here let a second click spawn another pipeline and the
    // _MAX_SESSIONS cap would then silently kill the oldest still-running
    // session.  _setSessionStatus() re-enables it once this mode has no live
    // session left.
    _appendLine(sess.id, `$ ${data.cmd}`, 'dim');
    _appendLine(sess.id, '', 'dim');
    _setSessionStatus(sess.id, 'running');
    _streamSession(sess.id);
  } catch (err) {
    _appendLine(sess.id, `[ERROR] ${err.message}`, 'err');
    _setSessionStatus(sess.id, 'error');
    _stopElapsedTimer(sess.id);
  }
}

const _SSE_RECONNECT_DELAYS = [2000, 4000, 8000, 15000, 30000, 60000, 120000, 180000];
const _SSE_MAX_RECONNECT = _SSE_RECONNECT_DELAYS.length;

function _isNoiseLine(line) {
  if (!line) return false;
  const t = line.trim();
  if (/^HTTP\/(1\.[01]|2)\s+\d{3}/.test(t)) return true;
  if (/^HTTP Request:\s+\w+\s+https?:\/\//i.test(t)) return true;
  if (/^HTTP Response:\s+\d{3}/i.test(t)) return true;
  if (/^DEBUG:(openai|httpx)\b/i.test(t)) return true;
  if (/^LiteLLM:DEBUG\b/i.test(t)) return true;
  if (/^LiteLLM\.utils:/i.test(t)) return true;
  if (/^LiteLLM\.proxy\.(client|utils):/i.test(t)) return true;
  return false;
}

function _streamSession(sessId, _reconnectCount = 0) {
  const sess = _getSession(sessId);
  if (!sess || !sess.run_id) return;
  _closeEvtSource(sessId);

  const resumeFrom = sess._linesReceived || 0;
  const evtSource = new EventSource(`/api/run/${sess.run_id}/stream?from=${resumeFrom}`);
  State._evtSources[sessId] = evtSource;

  // Mid-run reconnect alignment: when reconnecting with from=N > 0, the
  // [AWAIT_INPUT…] marker line may already be past the resume offset, so the
  // SSE replay will not re-emit it and _checkHumanInputSignal would never
  // fire — leaving the user looking at a frozen terminal with no banner and
  // no way to send the response.  Poll /api/run/<id> once and reconstruct the
  // banner state from the server-side awaiting_input / input_prompt fields.
  if (resumeFrom > 0 && sess.run_id) {
    fetch(`/api/run/${encodeURIComponent(sess.run_id)}`)
      .then(r => (r && r.ok) ? r.json() : null)
      .then(info => {
        if (!info || !info.awaiting_input) return;
        const prompt = (typeof info.input_prompt === 'string' && info.input_prompt.trim())
          ? info.input_prompt.trim() : 'Input required:';
        // Synthesise an [AWAIT_INPUT] marker so _checkHumanInputSignal applies
        // its existing banner-render logic without duplicating it here.  This
        // also benefits anyone listening to the marker for telemetry.
        _checkHumanInputSignal(sessId, `[AWAIT_INPUT: ${prompt}]`);
      })
      .catch(() => { /* non-critical — SSE replay may catch it */ });
  }

  const _WATCHDOG_MS = 10 * 60 * 1000;
  function _resetWatchdog() {
    if (State._evtTimers[sessId]) clearTimeout(State._evtTimers[sessId]);
    State._evtTimers[sessId] = setTimeout(async () => {
      _closeEvtSource(sessId);
      try {
        const check = await fetch(`/api/run/${sess.run_id}/status`);
        if (check.ok) {
          const info = await check.json();
          const terminal = info.status === 'done' || info.status === 'error' || info.status === 'cancelled';
          if (terminal) {
            const ok = info.returncode === 0;
            _setSessionStatus(sessId, info.status, info.returncode);
            _appendLine(sessId, ok
              ? '✅  Run completed successfully.'
              : `❌  Run exited with code ${info.returncode}.`, ok ? 'success' : 'err');
            return;
          }
        }
      } catch (_) {}
      if (_reconnectCount >= _SSE_MAX_RECONNECT) {
        _setSessionStatus(sessId, 'error');
        _appendLine(sessId, '[Connection lost after multiple retries. Run may still be active in background.]', 'warn');
        return;
      }
      const delay = _SSE_RECONNECT_DELAYS[_reconnectCount];
      _appendLine(sessId,
        `[No data for 10 min — reconnecting in ${delay / 1000}s\u2026 (attempt ${_reconnectCount + 1}/${_SSE_MAX_RECONNECT})]`, 'dim');
      setTimeout(() => _streamSession(sessId, _reconnectCount + 1), delay);
    }, _WATCHDOG_MS);
  }
  _resetWatchdog();

  evtSource.onmessage = (e) => {
    _reconnectCount = 0;
    _resetWatchdog();

    let raw;
    try { raw = JSON.parse(e.data); }
    catch (pe) { _appendLine(sessId, `[SSE parse error: ${pe.message}]`, 'warn'); return; }

    if (raw && typeof raw === 'object' && raw.__keepalive__) return;

    // __done__ is checked first so that a hypothetical future __done__ event
    // that also carries an error field is handled as a normal terminal event
    // rather than being misidentified as a stream error below.
    if (raw && typeof raw === 'object' && raw.__done__) {
      _closeEvtSource(sessId);
      const timedOut = raw.timeout === true;
      const ok = !timedOut && raw.returncode === 0;
      _setSessionStatus(sessId, ok ? 'done' : 'error', raw.returncode);
      if (timedOut) {
        _appendLine(sessId, '⚠️  Run timed out after 30 minutes with no output.', 'warn');
      } else {
        _appendLine(sessId, ok ? '✅  Run completed successfully.' : `❌  Run exited with code ${raw.returncode}.`, ok ? 'success' : 'err');
      }
      if (sess.agentStates) {
        Object.keys(sess.agentStates).forEach(k => {
          if (sess.agentStates[k] === 'active') sess.agentStates[k] = ok ? 'done' : 'error';
        });
        if (sessId === State.activeSession[sess.mode]) _refreshAgentFlow(sess.mode);
      }
      return;
    }

    // Backend sends {"error": "Run not found"} for stream errors — surface
    // them as readable error lines instead of falling through to String(raw)
    // which would render as the unhelpful "[object Object]".
    // Checked after __done__ so __done__ events always take priority.
    if (raw && typeof raw === 'object' && 'error' in raw) {
      _appendLine(sessId, `[SSE ERROR] ${raw.error}`, 'err');
      _closeEvtSource(sessId);
      _setSessionStatus(sessId, 'error', null);
      return;
    }

    const line = typeof raw === 'string' ? raw : String(raw);
    sess._linesReceived = (sess._linesReceived || 0) + 1;
    if (_isNoiseLine(line)) return;
    const cls  = _classifyLine(line);
    _appendLine(sessId, line, cls);
    _detectAgentActivity(sessId, line);
    _checkHumanInputSignal(sessId, line);
    if (sessId === State.activeSession[sess.mode] && State.activeView[sess.mode] === 'agentflow') {
      _refreshAgentFlow(sess.mode);
    }
  };

  evtSource.onerror = async () => {
    _closeEvtSource(sessId);
    try {
      const check = await fetch(`/api/run/${sess.run_id}/status`);
      if (check.ok) {
        const info = await check.json();
        const terminal = info.status === 'done' || info.status === 'error' || info.status === 'cancelled';
        if (terminal) {
          const ok = info.returncode === 0;
          _setSessionStatus(sessId, info.status, info.returncode);
          _appendLine(sessId, ok
            ? '✅  Run completed successfully.'
            : `❌  Run exited with code ${info.returncode}.`, ok ? 'success' : 'err');
          return;
        }
      }
    } catch (_) {}
    if (_reconnectCount >= _SSE_MAX_RECONNECT) {
      _setSessionStatus(sessId, 'error');
      _appendLine(sessId,
        `[Connection lost after ${_SSE_MAX_RECONNECT} retries. Run may still be active in background.]`, 'warn');
      return;
    }
    const delay = _SSE_RECONNECT_DELAYS[_reconnectCount];
    _appendLine(sessId,
      `[Reconnecting in ${delay / 1000}s\u2026 (attempt ${_reconnectCount + 1}/${_SSE_MAX_RECONNECT})]`, 'dim');
    setTimeout(() => _streamSession(sessId, _reconnectCount + 1), delay);
  };
}

async function killSession(mode) {
  const sess = _activeSession(mode);
  if (!sess) return;
  // v1.1.11 (F-A10): confirm only when the backend pipeline is actually still
  // running (avoid nagging on an already-finished session).
  if ((sess.status === 'running' || sess.status === 'starting') &&
      !confirm('Stop this run? The backend pipeline will be terminated.')) {
    return;
  }
  _closeEvtSource(sess.id);
  if (sess.run_id) await fetch(`/api/run/${sess.run_id}`, { method:'DELETE' }).catch(()=>{});
  _setSessionStatus(sess.id, 'cancelled');
  _appendLine(sess.id, '[Run stopped by user]', 'warn');
}

// Legacy compat
function killRun(mode) { return killSession(mode); }
function setRunStatus(mode, status) {
  const sess = _activeSession(mode);
  if (sess) _setSessionStatus(sess.id, status);
}
function appendTerminalLine(mode, text, cls='') {
  const sess = _activeSession(mode);
  if (sess) _appendLine(sess.id, text, cls);
}
function showTerminal(mode) {}
function clearTerminal(mode) { clearActiveSession(mode); }
function clearTerminalLines(mode) { clearActiveSession(mode); }

// ─── Dashboard ──────────────────────────────────────────────────────────────────
let chartsInited = false;

async function loadDashboard() {
  try {
    const resp = await fetch('/api/dashboard');
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();

    document.getElementById('stat-runs').textContent    = data.total_saved_runs ?? '—';
    // v1.0.5 round 4: render cost at 6 decimals to match the cost_tracker's
    // persistence precision (OpenRouter per-call costs reach the 6th
    // decimal; toFixed(4) silently truncated cheap-model spend to $0.0000).
    // Prefer the explicit ``total_cost_usd`` alias when present so future
    // wire-format changes that disambiguate USD from legacy cost-units do
    // not silently degrade the dashboard.
    const _costRaw = (data.total_cost_usd != null) ? data.total_cost_usd : data.total_cost;
    const _cost = Number(_costRaw);
    document.getElementById('stat-cost').textContent = (_costRaw != null && isFinite(_cost)) ? '$' + _cost.toFixed(6) : '—';
    const _qual = Number(data.avg_quality); document.getElementById('stat-quality').textContent = (data.avg_quality != null && isFinite(_qual)) ? _qual.toFixed(2) : '—';
    document.getElementById('stat-session').textContent = (data.session_runs || []).length;

    State._dashboardRuns = data.saved_runs || [];
    renderRunsTable(State._dashboardRuns);
    renderCharts(data);
  } catch (err) {
    console.error('Dashboard error:', err);
    // v1.1.11 (F-A2): surface the failure in-UI instead of leaving
    // "Loading run history…" forever (mirrors openRunDetail's err.message).
    const _wrap = document.getElementById('runs-table-wrap');
    if (_wrap) {
      _wrap.innerHTML =
        `<div class="empty-state"><div class="em-icon">❌</div>Failed to load dashboard — ${escHtml(err.message)}</div>`;
    }
    ['stat-runs', 'stat-cost', 'stat-quality'].forEach(id => {
      const cell = document.getElementById(id);
      if (cell) cell.textContent = '—';
    });
  }
  loadBudgetStatus();
  // v1.1.0: also refresh the run-insights widget so dashboard view is consistent.
  if (typeof loadInsightsDashboard === 'function') {
    try { loadInsightsDashboard(); } catch (_) {}
  }
}

// Client-side filtering of the runs table by search query
function filterRunsTable(q) {
  const runs = State._dashboardRuns;
  if (!q || !q.trim()) {
    renderRunsTable(runs);
    return;
  }
  const lower = q.trim().toLowerCase();
  renderRunsTable(runs.filter(r => r.id && r.id.toLowerCase().includes(lower)));
}

// v1.0.5: Render the quality-loop outcome badge based on the structured
// fields written by section_07 (run_meta.quality_passed +
// run_meta.quality_loop_failure_type, or review_report.passes /
// review_report.failure_type as fallback).  We deliberately do NOT
// substring-match "QUALITY_LOOP_GAVE_UP" against any free-form summary
// text — backend section_07 dropped that fallback in v1.0.5 round 3, and
// the frontend must not silently re-introduce it.
function _qualityBadgeHtml(passed, failureType) {
  const ft = (typeof failureType === 'string') ? failureType.trim().toUpperCase() : '';
  if (ft === 'QUALITY_LOOP_GAVE_UP') {
    return '<span class="quality-badge gaveup" title="Quality loop exhausted its retry budget without passing review">⚠ Gave up</span>';
  }
  if (passed === true) {
    return '<span class="quality-badge passed" title="Quality loop converged: review_report.passes = true">✓ Passed</span>';
  }
  if (passed === false) {
    return '<span class="quality-badge failed" title="Quality loop did not pass review (no structured failure_type)">✗ Failed</span>';
  }
  return '';  // null / undefined → run predates v1.0.5 fields, render nothing
}

function renderRunsTable(runs) {
  const wrap = document.getElementById('runs-table-wrap');
  const badge = document.getElementById('runs-count-badge');
  if (badge) badge.textContent = runs.length ? `${runs.length} run${runs.length !== 1 ? 's' : ''}` : '';
  if (!runs || runs.length === 0) {
    wrap.innerHTML = '<div class="empty-state"><div class="em-icon">📂</div>No saved runs found in <code>saved_projects/</code></div>';
    return;
  }
  const rows = runs.map(r => {
    const hasBt = r.has_backtest ? `<span style="color:var(--success);font-size:10px;">✓ BT</span>` : '';
    const qBadge = _qualityBadgeHtml(r.quality_passed, r.quality_loop_failure_type);
    const qNum = r.quality != null ? ((n => isFinite(n) ? n.toFixed(2) : '—')(Number(r.quality))) : '—';
    return `<tr style="cursor:pointer;" data-run-id="${escHtml(r.id)}" onclick="openRunDetail(this.dataset.runId)">
      <td class="mono">${escHtml(r.id)} ${hasBt}</td>
      <td>${r.cost != null ? ((n => isFinite(n) ? '$'+n.toFixed(6) : '—')(Number(r.cost))) : '—'}</td>
      <td>${qNum}${qBadge ? ' ' + qBadge : ''}</td>
      <td>${r.tokens != null ? ((n => isFinite(n) ? n.toLocaleString() : '—')(Number(r.tokens))) : '—'}</td>
      <td>${r.mtime ? new Date(r.mtime * 1000).toLocaleString() : '—'}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `
    <table class="run-table">
      <thead><tr>
        <th>Run ID</th><th>Cost (USD)</th><th>Quality</th><th>Tokens</th><th>Date</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderCharts(data) {
  Chart.defaults.color = '#7a91b8';
  Chart.defaults.borderColor = '#1c2e50';
  Chart.defaults.font.family = 'Inter';
  Chart.defaults.font.size = 11;

  // All chart canvases live on the same dashboard tab; if the first is missing
  // the rest won't be there either — avoid TypeError from .getContext() on null.
  if (!document.getElementById('chart-cost')) return;

  const costLabels = (data.saved_runs || []).slice(0,10).reverse().map(r => r.id.slice(0,8));
  const costVals   = (data.saved_runs || []).slice(0,10).reverse().map(r => r.cost || 0);
  destroyChart('chart-cost');
  const ctxCost = document.getElementById('chart-cost').getContext('2d');
  State.charts['chart-cost'] = new Chart(ctxCost, {
    type: 'bar',
    data: {
      labels: costLabels.length ? costLabels : ['—'],
      datasets: [{
        label: 'Cost (USD)',
        data: costVals.length ? costVals : [0],
        backgroundColor: 'rgba(99,102,241,0.6)',
        borderColor: '#6366f1',
        borderWidth: 1,
        borderRadius: 4,
      }]
    },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } } } }
  });

  const sessionRuns = data.session_runs || [];
  const done = sessionRuns.filter(r => r.status === 'done').length;
  const errs = sessionRuns.filter(r => r.status === 'error').length;
  const run  = sessionRuns.filter(r => r.status === 'running').length;
  const rest = Math.max(0, sessionRuns.length - done - errs - run);
  destroyChart('chart-status');
  const ctxStatus = document.getElementById('chart-status').getContext('2d');
  State.charts['chart-status'] = new Chart(ctxStatus, {
    type: 'doughnut',
    data: {
      labels: ['Done', 'Error', 'Running', 'Other'],
      datasets: [{
        data: sessionRuns.length === 0 ? [0, 0, 0, 1] : [done, errs, run, rest],
        backgroundColor: ['#22d3a0','#f87171','#6366f1','#3d5278'],
        borderWidth: 0,
        hoverOffset: 6,
      }]
    },
    options: { responsive: true, cutout: '65%', plugins: { legend: { position: 'bottom' } } }
  });

  const qualVals = (data.saved_runs || []).filter(r => r.quality != null).map(r => Number(r.quality)).filter(v => isFinite(v));
  const qualBuckets = [0,0,0,0,0];
  qualVals.forEach(v => { const i = Math.min(4, Math.max(0, Math.floor(v * 5))); qualBuckets[i]++; });
  destroyChart('chart-quality');
  const ctxQual = document.getElementById('chart-quality').getContext('2d');
  State.charts['chart-quality'] = new Chart(ctxQual, {
    type: 'bar',
    data: {
      labels: ['0–0.2','0.2–0.4','0.4–0.6','0.6–0.8','0.8–1.0'],
      datasets: [{
        label: 'Runs',
        data: qualBuckets,
        backgroundColor: ['#f87171','#fbbf24','#60a5fa','#34d399','#22d3a0'],
        borderWidth: 0, borderRadius: 4,
      }]
    },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { x: { grid: { display: false } } } }
  });

  destroyChart('chart-stages');
  const ctxStages = document.getElementById('chart-stages').getContext('2d');
  State.charts['chart-stages'] = new Chart(ctxStages, {
    type: 'radar',
    data: {
      labels: ['Bootstrap','Extraction','Research','Models','Web Research','Analysis','Self-Check'],
      datasets: [{
        label: 'Avg Stage Score',
        data: [0.9, 0.85, 0.8, 0.88, 0.75, 0.92, 0.87],
        fill: true,
        backgroundColor: 'rgba(99,102,241,0.15)',
        borderColor: '#6366f1',
        pointBackgroundColor: '#8b5cf6',
        pointRadius: 3,
      }]
    },
    options: {
      responsive: true,
      scales: { r: { min: 0, max: 1, ticks: { display: false }, grid: { color: '#1c2e50' }, pointLabels: { font: { size: 10 } } } },
      plugins: { legend: { display: false } }
    }
  });
}

function destroyChart(id) {
  if (State.charts[id]) { State.charts[id].destroy(); delete State.charts[id]; }
}

// ─── Leaderboard ────────────────────────────────────────────────────────────────

async function loadLeaderboard() {
  const sortBy    = document.getElementById('lb-sort-by').value;
  const modeFilter= document.getElementById('lb-mode-filter').value;
  const limit     = document.getElementById('lb-limit').value;

  const params = new URLSearchParams({ sort_by: sortBy, limit });
  if (modeFilter) params.set('mode', modeFilter);

  const wrap = document.getElementById('leaderboard-table-wrap');
  wrap.innerHTML = '<div class="empty-state"><div class="em-icon">⏳</div>Loading…</div>';

  try {
    const resp = await fetch('/api/leaderboard?' + params.toString());
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    if (data.error) {
      wrap.innerHTML = `<div class="empty-state"><div class="em-icon">⚠️</div>${escHtml(data.error)}</div>`;
      return;
    }
    State._lbData = data.runs || [];
    renderLeaderboardTable(State._lbData, sortBy);
  } catch (err) {
    wrap.innerHTML = `<div class="empty-state"><div class="em-icon">❌</div>${escHtml(err.message)}</div>`;
  }
}

function renderLeaderboardTable(runs, highlightCol) {
  const wrap = document.getElementById('leaderboard-table-wrap');
  if (!runs || runs.length === 0) {
    wrap.innerHTML = '<div class="empty-state"><div class="em-icon">🏆</div>No backtest data found. Run a pipeline with --backtest-runner to populate the leaderboard.</div>';
    return;
  }

  const fmtPct = v => { const n = Number(v); return (v != null && isFinite(n)) ? (n * 100).toFixed(2) + '%' : '—'; };
  const fmtF2  = v => { const n = Number(v); return (v != null && isFinite(n)) ? n.toFixed(2) : '—'; };
  const fmtI   = v => { const n = Number(v); return (v != null && isFinite(n)) ? n.toFixed(0) : '—'; };

  const rankClass = (rank) => {
    if (rank === 1) return 'gold';
    if (rank === 2) return 'silver';
    if (rank === 3) return 'bronze';
    return '';
  };

  const cols = [
    { key: 'rank',         label: '#',            fmt: r => {
        const rc = rankClass(r.rank);
        return `<span class="lb-rank ${rc}">${escHtml(String(r.rank))}</span>`;
    }, noSort: true },
    { key: 'run_id',       label: 'Run ID',        fmt: r => `<span class="mono" style="cursor:pointer;color:#a5b4fc;" data-run-id="${escHtml(r.run_id)}" onclick="openRunDetail(this.dataset.runId)">${escHtml(r.run_id)}</span>` },
    { key: 'mode',         label: 'Mode',          fmt: r => `<span style="color:var(--text-2)">${escHtml(r.mode)}</span>` },
    { key: 'sharpe_ratio', label: 'Sharpe',        fmt: r => `<span style="color:${r.sharpe_ratio != null && r.sharpe_ratio >= 0 ? 'var(--success)' : 'var(--text-2)'}">${fmtF2(r.sharpe_ratio)}</span>` },
    { key: 'max_drawdown', label: 'Max DD',        fmt: r => `<span style="color:${r.max_drawdown != null && r.max_drawdown > 0.2 ? 'var(--error)' : 'var(--text-2)'}">${fmtPct(r.max_drawdown)}</span>` },
    { key: 'total_return', label: 'Return %',      fmt: r => `<span style="color:${r.total_return != null && r.total_return >= 0 ? 'var(--success)' : 'var(--error)'}">${fmtPct(r.total_return)}</span>` },
    { key: 'win_rate',     label: 'Win Rate',      fmt: r => fmtPct(r.win_rate) },
    { key: 'score',        label: 'Quality',       fmt: r => r.score != null ? fmtF2(r.score) : '—' },
  ];

  const headers = cols.map(c => {
    if (c.noSort) return `<th>${c.label}</th>`;
    const isActive = c.key === (State._lbSortCol || highlightCol);
    const dirCls = isActive ? (State._lbSortAsc ? 'sort-asc' : 'sort-desc') : '';
    return `<th class="sortable ${dirCls}" data-col="${escHtml(c.key)}" onclick="_lbSort(this.dataset.col)">${c.label}</th>`;
  }).join('');

  const rows = runs.map(r =>
    `<tr>${cols.map(c => `<td>${c.fmt(r)}</td>`).join('')}</tr>`
  ).join('');

  wrap.innerHTML = `
    <table class="run-table">
      <thead><tr>${headers}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <div style="font-size:11px;color:var(--text-muted);margin-top:8px;text-align:right;">${runs.length} run${runs.length !== 1 ? 's' : ''} with backtest data</div>
  `;
}

// Client-side secondary sort for leaderboard table
function _lbSort(col) {
  if (!State._lbData || State._lbData.length === 0) return;
  if (State._lbSortCol === col) {
    State._lbSortAsc = !State._lbSortAsc;
  } else {
    State._lbSortCol = col;
    State._lbSortAsc = true;
  }
  const ascending_cols = new Set(['max_drawdown', 'rank']);
  const defaultAsc = ascending_cols.has(col);
  const asc = State._lbSortAsc ? defaultAsc : !defaultAsc;

  const sorted = [...State._lbData].sort((a, b) => {
    const av = a[col], bv = b[col];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === 'string' || typeof bv === 'string') return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
    const an = Number(av), bn = Number(bv);
    // NaN values sort to end regardless of direction
    if (isNaN(an) && isNaN(bn)) return 0;
    if (isNaN(an)) return 1;
    if (isNaN(bn)) return -1;
    return asc ? an - bn : bn - an;
  });
  // Recalculate rank without mutating the original State._lbData objects
  // (sorted is a shallow copy, so r references are shared — use map to create
  // new row objects with updated rank values).
  const ranked = sorted.map((r, idx) => ({ ...r, rank: idx + 1 }));
  renderLeaderboardTable(ranked, col);
}

// ─── Run Detail Modal ────────────────────────────────────────────────────────────

async function openRunDetail(runId) {
  // Track the current request so stale responses from a previous click are ignored.
  State._detailRunId = runId;

  const overlay = document.getElementById('run-detail-modal');
  document.getElementById('modal-run-id').textContent = runId;
  document.getElementById('modal-body').innerHTML = '<div class="empty-state"><div class="em-icon">⏳</div>Loading…</div>';
  // a11y (F-B2): remember the trigger so focus restores on close, move focus
  // into the dialog, and bind a Tab trap once per panel.
  State._modalLastFocused = document.activeElement;
  overlay.classList.add('open');
  const _panel = overlay.querySelector('.modal-panel');
  const _closeBtn = overlay.querySelector('.modal-close');
  if (_closeBtn) _closeBtn.focus();
  if (_panel && !_panel._a11yTrapBound) {
    _panel._a11yTrapBound = true;
    _panel.addEventListener('keydown', _trapModalTab);
  }

  try {
    // Load detail and backtest chart in parallel
    const [detailResp, chartResp] = await Promise.all([
      fetch(`/api/run/${encodeURIComponent(runId)}/detail`),
      fetch(`/api/run/${encodeURIComponent(runId)}/backtest-chart`),
    ]);

    // Discard if a newer openRunDetail() call was made while we were fetching
    if (State._detailRunId !== runId) return;

    if (!detailResp.ok) throw new Error(`Detail fetch error ${detailResp.status}`);
    if (!chartResp.ok) throw new Error(`Chart fetch error ${chartResp.status}`);
    const detail = await detailResp.json();
    const chartData = await chartResp.json();

    if (State._detailRunId !== runId) return;
    if (detail.error) throw new Error(detail.error);

    renderRunDetailModal(runId, detail, chartData);
  } catch (err) {
    if (State._detailRunId !== runId) return;
    document.getElementById('modal-body').innerHTML =
      `<div class="empty-state"><div class="em-icon">❌</div>${escHtml(err.message)}</div>`;
  }
}

function renderRunDetailModal(runId, detail, chartData) {
  const files = detail.files || {};
  const analysis  = files.analysis  || {};
  const meta      = files.meta      || {};
  const review    = files.review    || {};
  const backtest  = files.backtest  || {};
  const codeFiles = detail.code_files || [];

  // v1.0.5: Resolve the quality outcome from the structured fields ONLY.
  // ``run_meta.quality_passed`` (bool) is the canonical top-level field
  // promoted in v1.0.5 round 2; we fall back to ``review_report.passes`` /
  // ``review_report.failure_type`` for older saved_projects/ entries that
  // predate the promotion.  The frontend mirrors the backend's strict
  // validation: only ``QUALITY_LOOP_GAVE_UP`` is recognised as a structured
  // failure_type.  We deliberately avoid substring-matching the summary
  // (matching backend section_07's removal of that fallback in round 3).
  const qPassedRaw = (typeof meta.quality_passed === 'boolean')
    ? meta.quality_passed
    : (typeof review.passes === 'boolean' ? review.passes : null);
  const qFailureRaw =
    (typeof meta.quality_loop_failure_type === 'string' && meta.quality_loop_failure_type.trim())
      ? meta.quality_loop_failure_type
      : ((typeof review.failure_type === 'string' && review.failure_type.trim())
         ? review.failure_type : '');
  const qBadgeHtml = _qualityBadgeHtml(qPassedRaw, qFailureRaw);

  // Build KV grid for meta/analysis
  const kvPairs = [
    { label: 'Mode',         value: meta.mode || analysis.mode_used || '—' },
    { label: 'Provider',     value: meta.llm_provider || '—' },
    { label: 'Timestamp',    value: meta.timestamp || analysis.timestamp || '—' },
    { label: 'Risk Level',   value: analysis.risk_level || '—' },
    { label: 'Gate Decision',value: analysis.gate_decision || '—' },
    { label: 'Score',        value: (() => { const n = Number(analysis.score); return (analysis.score != null && isFinite(n)) ? n.toFixed(3) : '—'; })() },
    // v1.0.5 round 4: prefer the USD-explicit field (``total_cost_usd``)
    // promoted into run_meta.json by section_07.  Fall back to the legacy
    // ``total_cost`` key for older saved_projects/ that predate the
    // promotion.  Display at 6 decimals to match persistence precision.
    { label: 'Total Cost',   value: (() => {
        const raw = (meta.total_cost_usd != null) ? meta.total_cost_usd : meta.total_cost;
        if (raw == null) return '—';
        const n = Number(raw);
        return isFinite(n) ? '$' + n.toFixed(6) : '—';
      })() },
    { label: 'Total Tokens', value: (() => { const n = Number(meta.total_tokens); return (meta.total_tokens != null && isFinite(n)) ? n.toLocaleString() : '—'; })() },
  ];
  // Feature 8: schema_version
  const sv = analysis.schema_version || meta.schema_version;
  if (sv != null) kvPairs.push({ label: 'Schema Version', value: 'v' + sv });

  // The Quality Status cell is rendered as raw HTML (not escaped via
  // escHtml) so the badge span survives intact; build it separately and
  // splice into the KV grid as a final cell.
  const kvHtml = kvPairs.map(kv => `
    <div class="detail-kv">
      <div class="detail-kv-label">${escHtml(kv.label)}</div>
      <div class="detail-kv-value">${escHtml(String(kv.value))}</div>
    </div>`).join('') + (qBadgeHtml ? `
    <div class="detail-kv">
      <div class="detail-kv-label">Quality Status</div>
      <div class="detail-kv-value">${qBadgeHtml}</div>
    </div>` : '');

  // Backtest metrics
  let btHtml = '';
  if (chartData && chartData.has_data) {
    const s = chartData.summary || {};
    const fmtPct = v => { const n = Number(v); return (v != null && isFinite(n)) ? (n * 100).toFixed(2) + '%' : '—'; };
    const fmtF2  = v => { const n = Number(v); return (v != null && isFinite(n)) ? n.toFixed(3) : '—'; };
    const btKv = [
      { label: 'Sharpe Ratio',  value: fmtF2(s.sharpe_ratio) },
      { label: 'Max Drawdown',  value: fmtPct(s.max_drawdown) },
      { label: 'Total Return',  value: fmtPct(s.total_return) },
      { label: 'Win Rate',      value: fmtPct(s.win_rate) },
      { label: 'Trade Count',   value: (() => { const n = Number(s.trade_count); return (s.trade_count != null && isFinite(n)) ? n.toFixed(0) : '—'; })() },
      { label: 'Profit Factor', value: fmtF2(s.profit_factor) },
    ];
    btHtml = `<div class="detail-section">
      <div class="detail-section-title">Backtest Metrics</div>
      <div class="detail-kv-grid">${btKv.map(kv => `
        <div class="detail-kv">
          <div class="detail-kv-label">${escHtml(kv.label)}</div>
          <div class="detail-kv-value">${escHtml(kv.value)}</div>
        </div>`).join('')}
      </div>
    </div>`;

    // Equity curve chart placeholder — rendered after innerHTML assignment
    if (chartData.equity_curve && chartData.equity_curve.length > 1) {
      btHtml += `<div class="detail-section">
        <div class="detail-section-title">Equity Curve</div>
        <div class="detail-chart-wrap">
          <canvas id="modal-equity-chart"></canvas>
        </div>
      </div>`;
    }
    // Feature 5: Drawdown curve
    if (chartData.drawdown_curve && chartData.drawdown_curve.length > 1) {
      btHtml += `<div class="detail-section">
        <div class="detail-section-title">Drawdown Curve</div>
        <div class="detail-chart-wrap">
          <canvas id="modal-drawdown-chart"></canvas>
        </div>
      </div>`;
    }
    // Feature 5: Monthly returns heatmap
    if (chartData.monthly_returns && Object.keys(chartData.monthly_returns).length) {
      btHtml += `<div class="detail-section">
        <div class="detail-section-title">Monthly Returns Heatmap</div>
        <div id="modal-monthly-heatmap"></div>
      </div>`;
    }
  }

  // Code files
  const codeHtml = codeFiles.length
    ? `<div class="detail-code-list">${codeFiles.map(f =>
        `<span class="detail-code-chip">${escHtml(f)}</span>`
      ).join('')}</div>`
    : '<span style="color:var(--text-muted);font-size:12px;">No code files found</span>';

  // Analysis consensus excerpt (up to 400 chars)
  const consensusRaw = analysis.consensus || analysis.recommendation || '';
  const consensusHtml = consensusRaw
    ? `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:12px;color:var(--text-2);line-height:1.6;max-height:120px;overflow-y:auto;">${escHtml(String(consensusRaw).slice(0, 600))}${String(consensusRaw).length > 600 ? '…' : ''}</div>`
    : '';

  // v1.0.5: Review section — surface the structured review_report.json
  // payload (summary + issues) that backend section_07 emits.  Only
  // rendered when the file actually exists; older runs that predate the
  // file simply skip the section.
  const reviewSummaryRaw = (typeof review.summary === 'string') ? review.summary : '';
  const reviewSummaryHtml = reviewSummaryRaw
    ? `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:12px;color:var(--text-2);line-height:1.6;max-height:160px;overflow-y:auto;">${escHtml(String(reviewSummaryRaw).slice(0, 1200))}${String(reviewSummaryRaw).length > 1200 ? '…' : ''}</div>`
    : '';

  const issuesArr = Array.isArray(review.issues) ? review.issues : [];
  const _SEV_RANK = { high: 0, medium: 1, low: 2 };
  // Sort high → medium → low so the most actionable items appear first.
  const issuesSorted = issuesArr.slice().sort((a, b) => {
    const ra = _SEV_RANK[String((a && a.severity) || '').toLowerCase()] ?? 3;
    const rb = _SEV_RANK[String((b && b.severity) || '').toLowerCase()] ?? 3;
    return ra - rb;
  });
  const ISSUE_CAP = 20;
  const issuesShown = issuesSorted.slice(0, ISSUE_CAP);
  const issuesOverflow = Math.max(0, issuesSorted.length - ISSUE_CAP);
  const issuesHtml = issuesShown.length
    ? `<div class="review-issue-list">${issuesShown.map(it => {
        const sev = String((it && it.severity) || '').toLowerCase();
        const sevClass = (sev === 'high' || sev === 'medium' || sev === 'low')
          ? `review-issue-severity-${sev}` : 'review-issue-severity-unknown';
        const sevLabel = sev ? sev.toUpperCase() : '—';
        const file = (typeof it.file === 'string' && it.file.trim()) ? it.file : '';
        const desc = (typeof it.description === 'string') ? it.description : '';
        const sug  = (typeof it.suggestion === 'string') ? it.suggestion : '';
        const cat  = (typeof it.category === 'string' && it.category.trim()) ? it.category : '';
        return `<div class="review-issue-item">
          <div class="review-issue-head">
            <span class="review-issue-sev ${sevClass}">${escHtml(sevLabel)}</span>
            ${cat ? `<span class="review-issue-cat">${escHtml(cat)}</span>` : ''}
            ${file ? `<span class="review-issue-file mono">${escHtml(file)}</span>` : ''}
          </div>
          <div class="review-issue-desc">${escHtml(desc)}</div>
          ${sug ? `<div class="review-issue-sug"><span class="review-issue-sug-label">Suggestion</span> ${escHtml(sug)}</div>` : ''}
        </div>`;
      }).join('')}${issuesOverflow > 0 ? `<div class="review-issue-overflow">+${issuesOverflow} more issue${issuesOverflow !== 1 ? 's' : ''} not shown — see <code>review_report.json</code></div>` : ''}</div>`
    : '';

  document.getElementById('modal-body').innerHTML = `
    <div class="detail-section">
      <div class="detail-section-title">Run Info</div>
      <div class="detail-kv-grid">${kvHtml}</div>
    </div>
    ${consensusRaw ? `<div class="detail-section">
      <div class="detail-section-title">Analysis Consensus</div>
      ${consensusHtml}
    </div>` : ''}
    ${reviewSummaryHtml ? `<div class="detail-section">
      <div class="detail-section-title">Review Summary ${qBadgeHtml ? qBadgeHtml : ''}</div>
      ${reviewSummaryHtml}
    </div>` : ''}
    ${issuesHtml ? `<div class="detail-section">
      <div class="detail-section-title">Review Issues (${issuesSorted.length})</div>
      ${issuesHtml}
    </div>` : ''}
    ${btHtml}
    <div class="detail-section">
      <div class="detail-section-title">Code Files (${codeFiles.length})</div>
      ${codeHtml}
    </div>
  `;

  // Render equity chart if data exists
  if (chartData && chartData.equity_curve && chartData.equity_curve.length > 1) {
    const cvs = document.getElementById('modal-equity-chart');
    if (cvs) {
      if (State._detailChart) { State._detailChart.destroy(); State._detailChart = null; }
      const labels = chartData.equity_curve.map(p => p.ts);
      const values = chartData.equity_curve.map(p => p.equity);
      State._detailChart = new Chart(cvs.getContext('2d'), {
        type: 'line',
        data: {
          labels,
          datasets: [{
            label: 'Equity',
            data: values,
            borderColor: '#22d3a0',
            backgroundColor: 'rgba(34,211,160,0.08)',
            borderWidth: 1.5,
            pointRadius: 0,
            fill: true,
            tension: 0.3,
          }]
        },
        options: {
          responsive: true,
          animation: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
            y: { grid: { color: '#1c2e50' } },
          }
        }
      });
    }
  }

  // Feature 5: Render drawdown chart
  if (chartData && chartData.drawdown_curve && chartData.drawdown_curve.length > 1) {
    const ddCvs = document.getElementById('modal-drawdown-chart');
    if (ddCvs) {
      if (State._detailDrawdownChart) { State._detailDrawdownChart.destroy(); State._detailDrawdownChart = null; }
      const ddLabels = chartData.drawdown_curve.map(p => p.ts);
      // Backend sends {"ts": ..., "dd": float} — use p.dd (not p.drawdown)
      const ddValues = chartData.drawdown_curve.map(p => {
        const v = typeof p.dd === 'number' ? p.dd : (typeof p.drawdown === 'number' ? p.drawdown : null);
        return v != null ? v * 100 : null;
      });
      State._detailDrawdownChart = new Chart(ddCvs.getContext('2d'), {
        type: 'line',
        data: {
          labels: ddLabels,
          datasets: [{
            label: 'Drawdown %',
            data: ddValues,
            borderColor: '#f87171',
            backgroundColor: 'rgba(248,113,113,0.10)',
            borderWidth: 1.5,
            pointRadius: 0,
            fill: true,
            tension: 0.3,
          }]
        },
        options: {
          responsive: true,
          animation: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { maxTicksLimit: 8, maxRotation: 0 }, grid: { display: false } },
            y: {
              reverse: true,
              grid: { color: '#1c2e50' },
              ticks: { callback: v => v.toFixed(1) + '%' },
              title: { display: true, text: 'Max Drawdown %', color: '#7a91b8', font: { size: 10 } },
            },
          }
        }
      });
    }
  }

  // Feature 5: Render monthly returns heatmap
  if (chartData && chartData.monthly_returns && Object.keys(chartData.monthly_returns).length) {
    const hmWrap = document.getElementById('modal-monthly-heatmap');
    if (hmWrap) {
      hmWrap.innerHTML = _buildMonthlyHeatmap(chartData.monthly_returns);
    }
  }
}

// Build monthly returns heatmap HTML table
function _buildMonthlyHeatmap(monthlyReturns) {
  // monthlyReturns from backend: flat { "YYYY-MM": float, ... }
  // e.g. { "2023-01": 0.032, "2023-02": -0.015 }
  if (!monthlyReturns || typeof monthlyReturns !== 'object') return '';

  const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  // Convert flat YYYY-MM keys into nested { YYYY: { monthIndex: float } }
  const nested = {};
  Object.entries(monthlyReturns).forEach(([key, val]) => {
    // key may be "YYYY-MM" (backend) or "YYYY" (legacy nested outer key — handled below)
    const parts = String(key).split('-');
    if (parts.length >= 2) {
      const y = parts[0];
      const m = String(parseInt(parts[1], 10)); // "01" → "1"
      if (!nested[y]) nested[y] = {};
      nested[y][m] = val;
    } else if (parts.length === 1 && typeof val === 'object' && val !== null) {
      // Legacy nested format: { "2023": { "1": 0.032, ... } }
      nested[key] = val;
    }
  });

  const years = Object.keys(nested).sort();
  if (!years.length) return '';

  // Find min/max for colour scaling
  let minV = 0, maxV = 0;
  years.forEach(y => {
    Object.values(nested[y]).forEach(v => {
      const n = Number(v);
      if (isFinite(n)) { minV = Math.min(minV, n); maxV = Math.max(maxV, n); }
    });
  });
  const absMax = Math.max(Math.abs(minV), Math.abs(maxV), 0.001);

  function cellColor(v) {
    const n = Number(v);
    if (!isFinite(n) || n === 0) return 'transparent';
    const ratio = Math.min(1, Math.abs(n) / absMax);
    if (n > 0) return `rgba(34,211,160,${(0.15 + ratio * 0.65).toFixed(2)})`;
    return `rgba(248,113,113,${(0.15 + ratio * 0.65).toFixed(2)})`;
  }

  const headerCells = MONTHS.map(m => `<th>${m}</th>`).join('');
  const rows = years.map(y => {
    const mData = nested[y] || {};
    const cells = Array.from({ length: 12 }, (_, i) => {
      const key = String(i + 1);
      const v = mData[key];
      if (v == null) return `<td style="background:transparent;color:var(--text-muted);">—</td>`;
      const n = Number(v);
      const pct = isFinite(n) ? (n * 100).toFixed(2) + '%' : '—';
      const bg = cellColor(n);
      const cls = n > 0 ? 'heatmap-cell-pos' : n < 0 ? 'heatmap-cell-neg' : 'heatmap-cell-zero';
      return `<td class="${cls}" style="background:${bg};">${pct}</td>`;
    }).join('');
    return `<tr><th style="text-align:right;padding-right:10px;">${escHtml(y)}</th>${cells}</tr>`;
  }).join('');

  return `<div style="overflow-x:auto;">
    <table class="monthly-heatmap-table">
      <thead><tr><th></th>${headerCells}</tr></thead>
      <tbody>${rows}</tbody>
    </table>
  </div>`;
}

// a11y (F-B2): keep Tab focus inside the open run-detail dialog.  Bound once
// per panel via the _a11yTrapBound guard in openRunDetail.
function _trapModalTab(e) {
  if (e.key !== 'Tab') return;
  const panel = e.currentTarget;
  const focusables = panel.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
  );
  const visible = Array.prototype.filter.call(focusables, el => el.offsetParent !== null || el === panel);
  if (!visible.length) { e.preventDefault(); panel.focus(); return; }
  const first = visible[0], last = visible[visible.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

function closeRunDetailModal(event) {
  // Close on overlay click (not on panel itself) or explicit null call
  if (event && event.target !== document.getElementById('run-detail-modal')) return;
  document.getElementById('run-detail-modal').classList.remove('open');
  if (State._detailChart) { State._detailChart.destroy(); State._detailChart = null; }
  if (State._detailDrawdownChart) { State._detailDrawdownChart.destroy(); State._detailDrawdownChart = null; }
  // a11y (F-B2): restore focus to whatever opened the modal.
  if (State._modalLastFocused && typeof State._modalLastFocused.focus === 'function') {
    State._modalLastFocused.focus();
  }
  State._modalLastFocused = null;
}

// Close modal on Escape key
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeRunDetailModal(null);
});

// ─── Webhook status ──────────────────────────────────────────────────────────────

async function loadWebhookStatus() {
  try {
    const resp = await fetch('/api/webhook/status');
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    const configured = !!data.configured;

    const badge = document.getElementById('webhook-status-badge');
    const cardBadge = document.getElementById('webhook-status-card-badge');
    const html = configured
      ? `<span class="webhook-badge configured"><span class="webhook-dot"></span>Webhook Active</span>`
      : `<span class="webhook-badge unconfigured"><span class="webhook-dot"></span>Webhook Not Configured</span>`;

    if (badge) badge.innerHTML = html;
    if (cardBadge) cardBadge.innerHTML = html;
  } catch (_) {
    // Non-critical; silently ignore
  }
}

// ─── Settings ───────────────────────────────────────────────────────────────────

const SETTINGS_SCHEMA = [
  { id:'provider', title:'LLM Provider', icon:'⚡', open:true, keys:['LLM_PROVIDER'] },
  { id:'openrouter', title:'OpenRouter', icon:'🌐', open:false, provider:'openrouter',
    keys:['OPENROUTER_API_KEY','OPENROUTER_BASE_URL','OPENROUTER_PRIMARY_MODEL','OPENROUTER_DIRECTION_JUDGE_MODEL','OPENROUTER_LIBRARIAN_MODEL','OPENROUTER_LLM_TIMEOUT_SECONDS'] },
  { id:'alibaba', title:'Alibaba Coding Plan', icon:'☁️', open:false, provider:'alibaba_coding_plan',
    keys:['ALIBABA_CODING_PLAN_API_KEY','ALIBABA_CODING_PLAN_BASE_URL','ALIBABA_CODING_PLAN_PRIMARY_MODEL','ALIBABA_CODING_PLAN_DIRECTION_JUDGE_MODEL','ALIBABA_CODING_PLAN_LIBRARIAN_MODEL','ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS','ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS'] },
  { id:'ollama', title:'Ollama (Local LLM)', icon:'🦙', open:false, provider:'ollama',
    keys:['OLLAMA_BASE_URL','OLLAMA_PRIMARY_MODEL','OLLAMA_DIRECTION_JUDGE_MODEL','OLLAMA_LIBRARIAN_MODEL'] },
  { id:'output', title:'Output & Logging', icon:'📋', open:false,
    keys:['STRICT_JSON','COST_TRACE','LOCAL_CACHE','CRUCIBLE_LOG_LEVEL','CRUCIBLE_JSON_LOGS',
          'LIBRARIAN_INTER_QUERY_DELAY_SECONDS','LIBRARIAN_MAX_RESULTS_PER_QUERY','LIBRARIAN_MAX_CITATIONS','LIBRARIAN_MAX_QUERIES_PER_LANE',
          'LIBRARIAN_HTTP_TIMEOUT_SECONDS','LIBRARIAN_HTTP_MAX_BYTES','LIBRARIAN_MAX_VERIFIED_CITATIONS',
          'CODEX_ENTRYPOINT','CRUCIBLE_ENV_FILE'] },
  { id:'gate', title:'Direction & Gate Control', icon:'🎯', open:false,
    keys:['GATE_CONTROL_ENABLED','SELECTIVE_RERUN_ENABLED',
          'DIRECTION_REFINEMENT_ENABLED','DIRECTION_REFINEMENT_MAX_ITERATIONS','GATE_DIRECTION_FEEDBACK_ENABLED','SELECTIVE_RERUN_MAX_ATTEMPTS'] },
  // v1.1.8 — Direction Debate Audit Mode.  Adjacent to ``gate`` group
  // because the two control related decision-flow surfaces.  Eight keys
  // covering audit master switch, structural finding enforcement, isolation
  // mode, external critic, override semantics, consensus threshold, and the
  // two ledger per-stream toggles.
  { id:'debate_audit', title:'Direction Debate Audit', icon:'⚖️', open:false,
    keys:['CRUCIBLE_DEBATE_AUDIT_MODE','CRUCIBLE_DEBATE_REQUIRE_STRUCTURED_FINDINGS',
          'CRUCIBLE_DEBATE_ISOLATION_MODE','CRUCIBLE_DEBATE_EXTERNAL_CRITIC',
          'CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED','CRUCIBLE_DEBATE_CONSENSUS_RISK_THRESHOLD',
          'CRUCIBLE_DEBATE_CRITIC_MAX_ATTEMPTS',
          'CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING','CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT'] },
  // v1.1.8 extended — Direction Gate Tuning.  Single env-backed per-run
  // toggle (CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE) controls
  // whether the gate is allowed to degrade to low-confidence proceed
  // after N exhausted refinement iterations instead of force-none.
  // Orthogonal to debate_audit group — that one is observation-only,
  // this one changes the actual decision path.
  { id:'debate_resilience', title:'Direction Gate Tuning', icon:'🛡️', open:false,
    keys:['CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE',
          'CRUCIBLE_DEBATE_DEGRADE_AFTER_N_ITERATIONS'] },
  // v1.1.8 extended — Web Research Hardening.  Five sub-groups covering
  // disk cache (Q1), provider resilience (Q2/Q3/Q5/Q7/Q9), extra zero-auth
  // providers (Q4), and query quality (Q6/Q8/Q10/P2).  All defaults are
  // production-safe.  Operators rarely need to touch these once set.
  { id:'librarian_cache', title:'Librarian Search Cache', icon:'💾', open:false,
    keys:['LIBRARIAN_SEARCH_DISK_CACHE_ENABLED','LIBRARIAN_SEARCH_CACHE_PATH',
          'LIBRARIAN_SEARCH_CACHE_TTL_DDG_HOURS','LIBRARIAN_SEARCH_CACHE_TTL_GITHUB_HOURS',
          'LIBRARIAN_SEARCH_CACHE_TTL_ARXIV_HOURS','LIBRARIAN_SEARCH_CACHE_TTL_CONTEXT7_HOURS'] },
  { id:'librarian_resilience', title:'Librarian Provider Resilience', icon:'🌐', open:false,
    keys:['LIBRARIAN_PROVIDER_COOLDOWN_INITIAL_SECONDS','LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS',
          'LIBRARIAN_PROVIDER_FALLBACK_ENABLED','LIBRARIAN_ASYNC_FANOUT_ENABLED',
          'LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED','LIBRARIAN_PROVIDER_HEALTH_SUMMARY',
          'LIBRARIAN_HTTP2_ENABLED','LIBRARIAN_HTTP_KEEPALIVE_ENABLED'] },
  { id:'librarian_providers', title:'Librarian Extra Providers', icon:'🔌', open:false,
    keys:['LIBRARIAN_EXTRA_PROVIDERS'] },
  // v1.1.10 — Librarian Provider Auth.  Optional API tokens for the two
  // search providers that gate behaviour on credentials.  Both default to
  // a placeholder string in .env.example so the Settings UI surfaces an
  // input box; placeholder values are filtered out by the backend
  // ``_resolve_*_token`` helpers, so unconfigured operators keep the
  // anonymous behaviour bit-for-bit.
  { id:'librarian_auth', title:'Librarian Provider Auth', icon:'🔐', open:false,
    keys:['CONTEXT7_API_KEY','GITHUB_TOKEN'] },
  { id:'librarian_query_quality', title:'Librarian Query Quality', icon:'🎯', open:false,
    keys:['LIBRARIAN_DOMAIN_PINS_ENABLED','LIBRARIAN_DOMAIN_PINS_PATH',
          'LIBRARIAN_BILINGUAL_QUERY_EXPANSION','LIBRARIAN_BILINGUAL_QUERY_THRESHOLD',
          'LIBRARIAN_QUERY_TRANSLATE_MODEL','LIBRARIAN_CLAIM_ATTRIBUTION_DIRECTION_KEY'] },
  { id:'budget', title:'Budget & Cost Limits', icon:'💰', open:false,
    keys:['BUDGET_SOFT_COST_LIMIT','BUDGET_HARD_COST_LIMIT','BUDGET_MAX_TOTAL_TOKENS'] },
  { id:'convergence', title:'Convergence Guard', icon:'🔄', open:false,
    keys:['CONVERGENCE_MAX_ITERATIONS','CONVERGENCE_TIMEOUT_SECONDS','CONVERGENCE_STALE_THRESHOLD'] },
  { id:'retry', title:'Agent Retry & Backoff', icon:'🔁', open:false,
    keys:['AGENT_KICKOFF_RETRY_ATTEMPTS','AGENT_KICKOFF_RETRY_BACKOFF_SECONDS','AGENT_KICKOFF_RETRY_MAX_BACKOFF_SECONDS','AGENT_KICKOFF_RETRY_JITTER_RATIO'] },
  { id:'api_check', title:'API Version Check', icon:'🔍', open:false,
    keys:['API_VERSION_CHECK_ENABLED','API_VERSION_CHECK_MAX_LIBRARIES','API_VERSION_CHECK_TIMEOUT_SECONDS','API_VERSION_CHECK_CACHE_TTL_HOURS','API_VERSION_CHECK_SEVERITY_THRESHOLD'] },
  { id:'enhanced', title:'Enhanced Features', icon:'🚀', open:false,
    keys:['ENHANCED_SECURITY_SCAN','ENHANCED_DEPLOYMENT_ARTIFACTS','ENHANCED_PROJECT_MEMORY','ENHANCED_PROJECT_MEMORY_MAX_ENTRIES',
          'ENHANCED_GENERATE_TESTS','ENHANCED_GENERATE_TESTS_MAX_FILES','ENHANCED_API_AUTOPATCH',
          'ENHANCED_INDEPENDENT_VALIDATION','ENHANCED_INDEPENDENT_VALIDATION_LLM','ENHANCED_INDEPENDENT_VALIDATION_TIMEOUT',
          'ENHANCED_CI_OUTPUT','ENHANCED_WATCH_DEBOUNCE_SECONDS','ENHANCED_WATCH_TIMEOUT','ENHANCED_BATCH_MAX_WORKERS','ENHANCED_BATCH_TIMEOUT',
          'ENHANCED_AUTO_REMEDIATION','ENHANCED_AUTO_REMEDIATION_MAX_ROUNDS',
          'ENHANCED_DEPENDENCY_AUDIT','ENHANCED_HTML_REPORT','ENHANCED_CODE_QUALITY','ENHANCED_RUN_REGISTRY','ENHANCED_INTERACTIVE',
          'ENHANCED_DEDUP_CHECK','DEDUP_SIMILARITY_THRESHOLD','DEDUP_LOOKBACK_DAYS','DEDUP_MAX_CORPUS_RUNS',
          'ENHANCED_POST_CHAT','POST_CHAT_CONTEXT_CHARS','ENHANCED_AGENT_METRICS','ENHANCED_PROMPT_VERSION_LABEL',
          'ENHANCED_LOCKFILE_GEN','PROJECT_MEMORY_PROMPT_CHARS','PIPELINE_PROJECT_PROFILE'] },
  { id:'notify', title:'Notifications', icon:'🔔', open:false,
    keys:['NOTIFY_WEBHOOK_URL','NOTIFY_SLACK_WEBHOOK_URL','NOTIFY_DISCORD_WEBHOOK_URL','NOTIFY_ON_FAIL_ONLY'] },
  { id:'run_insights', title:'Run Insights Ledger', icon:'📚', open:false,
    keys:['CRUCIBLE_RUN_INSIGHTS_ENABLED','CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT','CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS',
          'CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE','CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS','CRUCIBLE_RUN_INSIGHTS_REDACT',
          'CRUCIBLE_RUN_INSIGHTS_BACKEND'] },
  { id:'extdata', title:'External Data Connectors', icon:'📡', open:false,
    keys:['ENHANCED_GITHUB_REPO','ALPHA_VANTAGE_API_KEY','ALPHA_VANTAGE_BASE_URL','FRED_API_KEY','FRED_BASE_URL','COINGECKO_BASE_URL','EXTERNAL_DATA_TIMEOUT','EXTERNAL_DATA_MAX_RETRIES',
          'GITHUB_ANALYZER_TIMEOUT','GITHUB_ANALYZER_MAX_RETRIES','GITHUB_ANALYZER_CACHE_TTL'] },
  { id:'docing', title:'Document Ingestion', icon:'📄', open:false,
    keys:['ENHANCED_INGEST_DOCS','ENHANCED_INGEST_DOCS_DIR','DOCUMENT_INGESTION_MAX_CHARS','DOCUMENT_INGESTION_TOTAL_CHARS'] },
  { id:'multilang', title:'Multi-Language Codegen', icon:'🌐', open:false,
    keys:['ENHANCED_MULTILANG_CODEGEN','ENHANCED_MULTILANG_LANGS','MULTILANG_MAX_FILES','MULTILANG_MAX_CHARS','MULTILANG_ENABLE_RUST'] },
  { id:'abtest', title:'A/B Testing', icon:'⚗️', open:false,
    keys:['AB_TEST_TIMEOUT','AB_TEST_PARALLEL'] },
  { id:'backtest', title:'Backtest & Optimisation', icon:'📈', open:false,
    keys:['ENHANCED_BACKTEST_RUNNER','BACKTEST_REQUIRE_REAL_DATA',
          'BACKTEST_MIN_REAL_DATA_ROWS','BACKTEST_DATA_CACHE_TTL_HOURS',
          'BACKTEST_DATA_CACHE_DIR','BACKTEST_DATA_MAX_STALENESS_DAYS',
          'BACKTEST_SYNTHETIC_SEED','BACKTEST_PARAM_SEED','BACKTEST_FETCH_HARD_TIMEOUT_SEC','BACKTEST_PREPARE_DATA_ONLY',
          'BACKTEST_PARALLEL_FETCH',
          'BACKTEST_PARAM_SEARCH','BACKTEST_BAYESIAN_N_TRIALS',
          'BACKTEST_SYMBOL','BACKTEST_DATA_SOURCE','BACKTEST_PERIOD','BACKTEST_INTERVAL',
          'BACKTEST_DATA_ROWS','BACKTEST_INITIAL_CAPITAL','BACKTEST_MAX_COMBOS',
          'BACKTEST_TARGET_METRIC','BACKTEST_FIX_MAX_ROUNDS','BACKTEST_TIMEOUT',
          'PORTFOLIO_REBALANCE_PERIOD','PORTFOLIO_RISK_FREE_RATE'] },
  { id:'quant_analytics', title:'Quant Analytics Suite', icon:'📊', open:false,
    keys:['ENHANCED_QUANT_ANALYTICS','ENHANCED_WALK_FORWARD','ENHANCED_SIGNIFICANCE_TEST',
          'ENHANCED_REGIME_DETECTION','ENHANCED_FACTOR_ANALYSIS','ENHANCED_TRANSACTION_COST',
          'ENHANCED_MONTE_CARLO','ENHANCED_TEARSHEET','ENHANCED_SIGNAL_ANALYSIS',
          'ENHANCED_RISK_ATTRIBUTION','ENHANCED_COINTEGRATION','ENHANCED_DYNAMIC_CORRELATION',
          'WALK_FORWARD_N_SPLITS','WALK_FORWARD_OOS_PCT','WALK_FORWARD_IS_PCT','WALK_FORWARD_MIN_TRAIN_BARS',
          'SIG_N_PERMUTATIONS','SIG_N_BOOTSTRAP','SIG_CONFIDENCE_LEVEL',
          'REGIME_METHOD','REGIME_N_REGIMES','REGIME_VOL_WINDOW','REGIME_TREND_WINDOW','REGIME_LOOKBACK_BARS',
          'MC_N_SIMULATIONS','MC_HORIZON_DAYS','MC_METHOD','MC_SEED',
          'FACTOR_RISK_FREE_RATE','FACTOR_LOOKBACK_DAYS','FACTOR_USE_FF_DATA',
          'SIGNAL_HORIZONS','SIGNAL_MIN_OBSERVATIONS','SIGNAL_SIGNIFICANCE_THRESH',
          'RISK_METHOD','RISK_CONFIDENCE_LEVEL','RISK_LOOKBACK_WINDOW'] },
  { id:'tc', title:'Transaction Cost Model', icon:'💸', open:false,
    keys:['TC_COMMISSION_PCT','TC_SLIPPAGE_PCT','TC_SPREAD_BPS',
          'TC_USE_KYLE_IMPACT','TC_KYLE_LAMBDA','TC_AVG_DAILY_VOLUME','TC_N_SCENARIOS'] },
  { id:'tearsheet', title:'Tearsheet Report', icon:'📑', open:false,
    keys:['TEARSHEET_MONTHLY_RETURNS','TEARSHEET_DRAWDOWN_PERIODS',
          'TEARSHEET_MAX_DRAWDOWN_PERIODS','TEARSHEET_TRADE_ANALYSIS'] },
  { id:'mlflow', title:'MLflow Tracking', icon:'🧪', open:false,
    keys:['MLFLOW_TRACKING_URI','MLFLOW_EXPERIMENT_NAME','MLFLOW_LOG_ARTIFACTS'] },
  { id:'webhook', title:'Webhook Trigger', icon:'🔗', open:false,
    keys:['WEBHOOK_SECRET'] },
];

const KEY_META = {
  LLM_PROVIDER:          { label:'Provider',                  desc:{en:'Which provider powers all LLM calls in this run.', zh:'本次執行所有 LLM 呼叫使用的供應商。'},                                  type:'select', opts:[{v:'openrouter',l:'OpenRouter'},{v:'alibaba_coding_plan',l:'Alibaba Coding Plan'},{v:'ollama',l:'Ollama (Local)'}] },
  OPENROUTER_API_KEY:               { label:'API Key',                      desc:{en:'OpenRouter API key (openrouter.ai/keys).', zh:'OpenRouter API 金鑰（openrouter.ai/keys）。'},                                          type:'password' },
  OPENROUTER_BASE_URL:              { label:'Base URL',                     desc:{en:'OpenRouter API endpoint — every OpenRouter request uses this URL. Override if you use a proxy.', zh:'OpenRouter API 端點。所有 OpenRouter 請求使用此 URL。如使用代理可修改。'},             type:'text' },
  OPENROUTER_PRIMARY_MODEL:         { label:'Primary Model',                desc:{en:'Main analysis model for the workflow pipeline.', zh:'工作流主要分析使用的模型。'},                                    type:'text' },
  OPENROUTER_DIRECTION_JUDGE_MODEL: { label:'Direction Judge Model',        desc:{en:'Stage 0 direction debate judge model.', zh:'Stage 0 方向辯論的評審模型。'},                                             type:'text' },
  OPENROUTER_LIBRARIAN_MODEL:       { label:'Librarian / Research Model',   desc:{en:'Research and document retrieval model.', zh:'研究與文件檢索使用的模型。'},                                            type:'text' },
  OPENROUTER_LLM_TIMEOUT_SECONDS:   { label:'Request Timeout (s)',          desc:{en:'Max seconds per OpenRouter LLM request before timeout.', zh:'單次 OpenRouter LLM 請求逾時前的最大秒數。'},                           type:'number' },
  ALIBABA_CODING_PLAN_API_KEY:               { label:'API Key',             desc:{en:'Alibaba DashScope API key for Coding Plan.', zh:'Alibaba DashScope Coding Plan 的 API 金鑰。'},                                        type:'password' },
  ALIBABA_CODING_PLAN_BASE_URL:              { label:'Base URL',            desc:{en:'OpenAI-compatible endpoint. Change only if using a proxy.', zh:'OpenAI 相容端點。僅在使用代理時修改。'},                         type:'text' },
  ALIBABA_CODING_PLAN_PRIMARY_MODEL:         { label:'Primary Model',       desc:{en:'Main analysis model.', zh:'主要分析使用的模型。'},                                                              type:'text' },
  ALIBABA_CODING_PLAN_DIRECTION_JUDGE_MODEL: { label:'Direction Judge',     desc:{en:'Stage 0 direction debate judge model.', zh:'Stage 0 方向辯論的評審模型。'},                                             type:'text' },
  ALIBABA_CODING_PLAN_LIBRARIAN_MODEL:       { label:'Librarian Model',     desc:{en:'Research and document retrieval model.', zh:'研究與文件檢索使用的模型。'},                                            type:'text' },
  ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS:  { label:'Request Timeout (s)', desc:{en:'Max seconds per Alibaba LLM request before timeout. Default: 180.', zh:'單次 Alibaba LLM 請求逾時前的最大秒數。預設 180。'},                  type:'number' },
  ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS: { label:'Initial Response Timeout (s)', desc:{en:'Max seconds to wait for the first token from Alibaba. Default: 120.', zh:'等待 Alibaba 回傳第一個 token 的最大秒數。預設 120。'}, type:'number' },
  OLLAMA_BASE_URL:               { label:'Ollama Base URL',             desc:{en:'Ollama server API base URL. Default: http://localhost:11434/v1.', zh:'Ollama 伺服器 API 基底 URL。預設 http://localhost:11434/v1。'},                  type:'text' },
  OLLAMA_PRIMARY_MODEL:          { label:'Primary Model',               desc:{en:'Main analysis model (must be pulled: ollama pull <model>).', zh:'主要分析使用的模型（需先用 ollama pull <model> 拉取）。'},                       type:'text' },
  OLLAMA_DIRECTION_JUDGE_MODEL:  { label:'Direction Judge Model',       desc:{en:'Stage 0 direction debate judge model.', zh:'Stage 0 方向辯論的評審模型。'},                                             type:'text' },
  OLLAMA_LIBRARIAN_MODEL:        { label:'Librarian / Research Model',  desc:{en:'Research and document retrieval model.', zh:'研究與文件檢索使用的模型。'},                                            type:'text' },
  STRICT_JSON:         { label:'Strict JSON',              desc:{en:'Force all LLM responses to be valid JSON.', zh:'強制所有 LLM 回應為合法 JSON。'},                type:'boolean' },
  COST_TRACE:          { label:'Cost Trace',               desc:{en:'Log per-call token costs to the output stream.', zh:'將每次呼叫的 token 成本記錄到輸出串流。'},           type:'boolean' },
  LOCAL_CACHE:         { label:'Local Cache',              desc:{en:'Cache LLM responses to disk to save cost on reruns.', zh:'將 LLM 回應快取到磁碟，重跑時節省成本。'},      type:'boolean' },
  CRUCIBLE_LOG_LEVEL:  { label:'Log Level',              desc:{en:'Python logging verbosity level.', zh:'Python logging 詳細程度。'},                          type:'select', opts:[{v:'DEBUG',l:'DEBUG'},{v:'INFO',l:'INFO'},{v:'WARNING',l:'WARNING'},{v:'ERROR',l:'ERROR'}] },
  CRUCIBLE_JSON_LOGS:  { label:'JSON Logs',              desc:{en:'Emit logs in structured JSON format (for log aggregators).', zh:'以結構化 JSON 格式輸出 log（供 log 聚合工具使用）。'}, type:'boolean' },
  LIBRARIAN_INTER_QUERY_DELAY_SECONDS: { label:'Librarian Search Delay (s)', desc:{en:'Minimum delay in seconds between consecutive web-search requests (min 0.5). Increase to 8–15 if DuckDuckGo returns 429 errors. Default: 4.', zh:'連續網路搜尋請求之間的最小間隔秒數（最低 0.5）。若 DuckDuckGo 回傳 429，調高到 8–15。預設 4。'}, type:'number' },
  LIBRARIAN_MAX_RESULTS_PER_QUERY:     { label:'Max Results per Query',      desc:{en:'Max search results fetched per individual web query. Default: 3.', zh:'單次網路查詢抓取的搜尋結果上限。預設 3。'},                type:'number' },
  LIBRARIAN_MAX_CITATIONS:             { label:'Max Citations',              desc:{en:'Upper bound of raw citation candidates collected. Default: 12.', zh:'蒐集的原始引用候選數量上限。預設 12。'},                   type:'number' },
  LIBRARIAN_MAX_QUERIES_PER_LANE:      { label:'Max Queries per Lane',       desc:{en:'Max search queries fired per research lane. Default: 4.', zh:'每條研究 lane 發出的搜尋查詢上限。預設 4。'},                          type:'number' },
  LIBRARIAN_HTTP_TIMEOUT_SECONDS:      { label:'HTTP Timeout (s)',           desc:{en:'Timeout for each citation-fetch HTTP request. Default: 15.', zh:'每筆引用 HTTP 抓取請求的逾時秒數。預設 15。'},                       type:'number' },
  LIBRARIAN_HTTP_MAX_BYTES:            { label:'HTTP Max Bytes',             desc:{en:'Max bytes downloaded per citation URL. Default: 1048576 (1 MB).', zh:'每個引用 URL 的最大下載 bytes。預設 1048576（1 MB）。'},                  type:'number' },
  LIBRARIAN_MAX_VERIFIED_CITATIONS:    { label:'Max Verified Citations',     desc:{en:'Max citations kept after verification pass. Default: 6.', zh:'通過驗證後保留的引用上限。預設 6。'},                          type:'number' },
  CODEX_ENTRYPOINT:          { label:'Runtime Validation Entrypoint', desc:{en:'Optional override for the runtime validation entry point (e.g. api/main.py:app). Leave blank to use auto-detection.', zh:'執行時驗證 entry point 的可選覆寫（例如 api/main.py:app）。留空使用自動偵測。'}, type:'text' },
  CRUCIBLE_ENV_FILE:  { label:'.env File Path',                desc:{en:'Optional override for the .env file location. Leave blank to use the default (.env in project root).', zh:'.env 檔案路徑的可選覆寫。留空使用預設（專案根目錄下的 .env）。'}, type:'text' },
  GATE_CONTROL_ENABLED:              { label:'Gate Controller',             desc:{en:'Master switch for the Gate Controller subsystem. Set to false to skip Gate evaluation entirely on every run (default: on).', zh:'Gate Controller 子系統主開關。設為 false 後所有執行皆跳過 Gate 評估（預設開啟）。'}, type:'boolean' },
  SELECTIVE_RERUN_ENABLED:           { label:'Selective Rerun',             desc:{en:'Master switch for the Selective Rerun subsystem. Set to false to disable all gate-triggered reruns even when Gate Controller is enabled (default: on).', zh:'Selective Rerun 子系統主開關。即使 Gate Controller 開啟，設為 false 也會停用所有 gate 觸發的重跑（預設開啟）。'}, type:'boolean' },
  DIRECTION_REFINEMENT_ENABLED:      { label:'Direction Refinement',        desc:{en:'Enable evidence-gap refinement inside Direction Debate.', zh:'在方向辯論內啟用證據缺口修補。'},    type:'boolean' },
  DIRECTION_REFINEMENT_MAX_ITERATIONS: { label:'Max Iterations',            desc:{en:'Max refinement rounds before accepting current direction.', zh:'接受當前方向前的最大修補輪數。'},  type:'number' },
  GATE_DIRECTION_FEEDBACK_ENABLED:   { label:'Gate Feedback',               desc:{en:'Allow Gate Controller to bounce analysis back for refinement.', zh:'允許 Gate Controller 將分析退回重新修補。'}, type:'boolean' },
  SELECTIVE_RERUN_MAX_ATTEMPTS:      { label:'Selective Rerun Max',         desc:{en:'Upper bound for Gate-triggered selective reruns.', zh:'Gate 觸發的 selective rerun 次數上限。'},           type:'number' },
  BUDGET_SOFT_COST_LIMIT:            { label:'Soft Cost Limit (USD)',       desc:{en:'Cumulative spend warning threshold in USD. Pipeline logs a WARNING when exceeded but continues. Leave blank to disable.', zh:'累計花費警告閾值（USD）。超過時管線會 log WARNING 但繼續執行。留空停用。'}, type:'number' },
  BUDGET_HARD_COST_LIMIT:            { label:'Hard Cost Limit (USD)',       desc:{en:'Cumulative spend hard cutoff in USD. Pipeline stops immediately with BudgetExceededError when exceeded. Leave blank to disable.', zh:'累計花費硬性上限（USD）。超過時管線立即拋出 BudgetExceededError 中止。留空停用。'}, type:'number' },
  BUDGET_MAX_TOTAL_TOKENS:           { label:'Max Total Tokens',            desc:{en:'Maximum total tokens (input + output) allowed per run across all LLM calls. Leave blank to disable.', zh:'單次執行所有 LLM 呼叫的 token 上限（input + output 總和）。留空停用。'}, type:'number' },
  CONVERGENCE_MAX_ITERATIONS:        { label:'Max Agent Iterations',        desc:{en:'Hard cap on agent tick() calls per run. 0 = disabled. Default: 50.', zh:'單次執行內 agent tick() 呼叫的硬性上限。0 = 停用。預設 50。'}, type:'number' },
  CONVERGENCE_TIMEOUT_SECONDS:       { label:'Agent Timeout (s)',           desc:{en:'Wall-clock timeout in seconds for the convergence guard. 0 = disabled. Default: 3600 (1 hour).', zh:'收斂守護的 wall-clock 逾時秒數。0 = 停用。預設 3600（1 小時）。'}, type:'number' },
  CONVERGENCE_STALE_THRESHOLD:       { label:'Stale Output Threshold',      desc:{en:'Emit StaleLoopWarning when the same agent output signature repeats this many times consecutively. 0 = disabled. Default: 5.', zh:'相同 agent 輸出 signature 連續重複此次數時發出 StaleLoopWarning。0 = 停用。預設 5。'}, type:'number' },
  AGENT_KICKOFF_RETRY_ATTEMPTS:        { label:'Max Retry Attempts',          desc:{en:'Maximum number of retry attempts for agent crew kickoff failures. Default: 20.', zh:'agent crew kickoff 失敗時的最大重試次數。預設 20。'}, type:'number' },
  AGENT_KICKOFF_RETRY_BACKOFF_SECONDS: { label:'Base Backoff (s)',            desc:{en:'Initial backoff delay in seconds between retries. Doubles each attempt up to max. Default: 2.0.', zh:'重試間的初始 backoff 秒數，每次嘗試後翻倍至最大值。預設 2.0。'}, type:'number' },
  AGENT_KICKOFF_RETRY_MAX_BACKOFF_SECONDS: { label:'Max Backoff (s)',         desc:{en:'Upper bound for exponential backoff delay. Default: 30.0.', zh:'指數 backoff 延遲的上限。預設 30.0。'},  type:'number' },
  AGENT_KICKOFF_RETRY_JITTER_RATIO:  { label:'Jitter Ratio',                desc:{en:'Random jitter added to backoff (0.0–1.0). Prevents thundering-herd. Default: 0.15.', zh:'加在 backoff 上的隨機抖動比例（0.0–1.0），避免 thundering herd。預設 0.15。'}, type:'number' },
  API_VERSION_CHECK_ENABLED:         { label:'Enabled',                     desc:{en:'Check for deprecated API calls after code generation.', zh:'程式碼生成後檢查是否使用已棄用的 API。'},      type:'boolean' },
  API_VERSION_CHECK_MAX_LIBRARIES:   { label:'Max Libraries',               desc:{en:'Max libraries to check per run.', zh:'單次執行檢查的套件數上限。'},                            type:'number' },
  API_VERSION_CHECK_TIMEOUT_SECONDS: { label:'Timeout (s)',                 desc:{en:'HTTP timeout for version check requests.', zh:'版本檢查 HTTP 請求的逾時秒數。'},                   type:'number' },
  API_VERSION_CHECK_CACHE_TTL_HOURS: { label:'Cache TTL (h)',               desc:{en:'How long to cache version check results.', zh:'版本檢查結果的快取存活時間（小時）。'},                   type:'number' },
  API_VERSION_CHECK_SEVERITY_THRESHOLD: { label:'Severity Threshold',       desc:{en:'Minimum severity to flag.', zh:'標記問題的最低嚴重程度。'},                                  type:'select', opts:[{v:'low',l:'Low'},{v:'medium',l:'Medium'},{v:'high',l:'High'}] },
  ENHANCED_SECURITY_SCAN:            { label:'Security Scan',               desc:{en:'Run static security analysis (bandit) on generated code.', zh:'對產出程式碼執行靜態安全分析（bandit）。'},   type:'boolean' },
  ENHANCED_DEPLOYMENT_ARTIFACTS:     { label:'Deployment Artifacts',        desc:{en:'Generate Dockerfile, docker-compose, CI workflow after run.', zh:'執行結束後產生 Dockerfile、docker-compose、CI workflow。'},type:'boolean' },
  ENHANCED_PROJECT_MEMORY:           { label:'Project Memory',              desc:{en:'Persist direction decisions and failed experiments across runs.', zh:'跨執行保存方向決策與失敗實驗。'}, type:'boolean' },
  ENHANCED_PROJECT_MEMORY_MAX_ENTRIES: { label:'Memory Max Entries',        desc:{en:'Max entries retained (oldest evicted when exceeded).', zh:'保留的最大記錄數（超過時淘汰最舊的）。'},       type:'number' },
  ENHANCED_GENERATE_TESTS:           { label:'Generate Tests',              desc:{en:'Generate pytest suites for each produced Python file.', zh:'為每個產出的 Python 檔產生 pytest 測試套件。'},       type:'boolean' },
  ENHANCED_GENERATE_TESTS_MAX_FILES: { label:'Max Files',                   desc:{en:'Max source files to generate tests for per run.', zh:'單次執行最多為幾個原始碼檔案產生測試。'},            type:'number' },
  ENHANCED_API_AUTOPATCH:            { label:'API Autopatch',               desc:{en:'Auto-patch deprecated API calls found by version check.', zh:'自動修補版本檢查找到的已棄用 API 呼叫。'},    type:'boolean' },
  ENHANCED_INDEPENDENT_VALIDATION:   { label:'Independent Validation',      desc:{en:'Run syntax/pytest/smoke in a subprocess after code gen.', zh:'程式碼生成後在 subprocess 內執行語法 / pytest / smoke 檢查。'},    type:'boolean' },
  ENHANCED_INDEPENDENT_VALIDATION_LLM: { label:'Validation LLM Review',    desc:{en:'Add adversarial LLM code review pass during validation.', zh:'驗證階段加入對抗式 LLM code review。'},    type:'boolean' },
  ENHANCED_INDEPENDENT_VALIDATION_TIMEOUT: { label:'Validation Timeout (s)',desc:{en:'Subprocess timeout for pytest and smoke check phases.', zh:'pytest 與 smoke 檢查階段的 subprocess 逾時秒數。'},      type:'number' },
  ENHANCED_CI_OUTPUT:                { label:'CI Output',                   desc:{en:'Write github_annotations.txt and ci_summary.md after run.', zh:'執行結束後寫出 github_annotations.txt 與 ci_summary.md。'},  type:'boolean' },
  ENHANCED_WATCH_DEBOUNCE_SECONDS:   { label:'Watch Debounce (s)',          desc:{en:'File-change debounce delay for watch subcommand.', zh:'watch 子指令的檔案變更 debounce 延遲秒數。'},           type:'number' },
  ENHANCED_WATCH_TIMEOUT:            { label:'Watch Run Timeout (s)',       desc:{en:'Per-triggered-run subprocess timeout for watch subcommand. Kills hung run and resumes watching. Default: 3600.', zh:'watch 子指令每次觸發 run 的 subprocess 逾時秒數。卡住的 run 會被終止並繼續監看。預設 3600。'}, type:'number' },
  ENHANCED_BATCH_MAX_WORKERS:        { label:'Batch Max Workers',           desc:{en:'Max parallel workers for batch subcommand (1 = sequential).', zh:'batch 子指令的最大平行 worker 數（1 = 序列執行）。'}, type:'number' },
  ENHANCED_AUTO_REMEDIATION:         { label:'Auto Remediation',            desc:{en:'LLM-driven closed-loop fix for HIGH+ security findings.', zh:'對 HIGH 以上嚴重程度的安全發現進行 LLM 驅動的閉環修復。'},    type:'boolean' },
  ENHANCED_AUTO_REMEDIATION_MAX_ROUNDS: { label:'Max Rounds',               desc:{en:'Max fix iterations before giving up.', zh:'放棄前的最大修復輪數。'},                       type:'number' },
  ENHANCED_DEPENDENCY_AUDIT:         { label:'Dependency Audit',            desc:{en:'Run pip-audit on generated requirements.txt for CVEs.', zh:'對產出的 requirements.txt 執行 pip-audit 檢查 CVE。'},      type:'boolean' },
  ENHANCED_HTML_REPORT:              { label:'HTML Report',                 desc:{en:'Generate a self-contained HTML run report.', zh:'產生獨立的 HTML 執行報告。'},                 type:'boolean' },
  ENHANCED_CODE_QUALITY:             { label:'Code Quality',                desc:{en:'Run AST-based complexity/LOC/nesting analysis.', zh:'執行 AST 為基礎的複雜度 / 行數 / 巢狀深度分析。'},             type:'boolean' },
  ENHANCED_RUN_REGISTRY:             { label:'Run Registry',                desc:{en:'Index completed runs into a SQLite registry.', zh:'將完成的 run 索引到 SQLite registry。'},               type:'boolean' },
  ENHANCED_INTERACTIVE:              { label:'Interactive Mode',            desc:{en:'Pause before each run for research guidance via stdin.', zh:'每次執行前透過 stdin 暫停，等待研究指引輸入。'},     type:'boolean' },
  ENHANCED_DEDUP_CHECK:              { label:'Dedup Check',                 desc:{en:'Detect semantically similar past runs before starting.', zh:'執行前偵測語意上相似的歷史 run。'},     type:'boolean' },
  DEDUP_SIMILARITY_THRESHOLD:        { label:'Similarity Threshold',        desc:{en:'Cosine similarity threshold [0.0–1.0] to flag as duplicate.', zh:'標記為重複的 cosine 相似度閾值（0.0–1.0）。'}, type:'number' },
  DEDUP_LOOKBACK_DAYS:               { label:'Lookback Days',               desc:{en:'Only compare runs within last N days (0 = no limit).', zh:'僅比對最近 N 天內的 run（0 = 不限制）。'},      type:'number' },
  DEDUP_MAX_CORPUS_RUNS:             { label:'Max Corpus Runs',             desc:{en:'Max past runs included in the similarity corpus.', zh:'納入相似度比對 corpus 的歷史 run 數量上限。'},           type:'number' },
  ENHANCED_BATCH_TIMEOUT:            { label:'Batch Timeout (s)',           desc:{en:'Per-project subprocess timeout for the batch subcommand. Default: 3600.', zh:'batch 子指令每個專案的 subprocess 逾時秒數。預設 3600。'},  type:'number' },
  ENHANCED_POST_CHAT:                { label:'Post-Analysis Chat',          desc:{en:'Start interactive Q&A about the analysis after the run completes. Enable with --post-chat.', zh:'執行結束後啟動與分析結果互動 Q&A 的介面。需搭配 --post-chat 開啟。'}, type:'boolean' },
  POST_CHAT_CONTEXT_CHARS:           { label:'Post-Chat Context (chars)',    desc:{en:'Max chars of analysis output injected as context for post-analysis chat. Default: 12000.', zh:'注入分析後對話作為 context 的分析輸出字元數上限。預設 12000。'}, type:'number' },
  ENHANCED_AGENT_METRICS:            { label:'Agent Metrics',               desc:{en:'Compute and display per-agent token, latency, and task metrics after the run. Enable with --agent-metrics.', zh:'執行結束後計算並顯示每個 agent 的 token、延遲、任務指標。需搭配 --agent-metrics 開啟。'}, type:'boolean' },
  ENHANCED_PROMPT_VERSION_LABEL:     { label:'Prompt Version Label',        desc:{en:'Label recorded alongside the run quality score for prompt A/B comparisons. Enable with --prompt-version-label.', zh:'與 run 品質分數一起記錄的標籤，用於 prompt A/B 比較。需搭配 --prompt-version-label 開啟。'}, type:'text' },
  ENHANCED_LOCKFILE_GEN:             { label:'Lock-file Generation',        desc:{en:'Generate pyproject.toml + pinned requirements.txt for generated code. Enable with --lockfile-gen.', zh:'為產出程式碼產生 pyproject.toml 與 pinned requirements.txt。需搭配 --lockfile-gen 開啟。'}, type:'boolean' },
  PROJECT_MEMORY_PROMPT_CHARS:       { label:'Memory Prompt Budget (chars)', desc:{en:'Max characters of project memory injected into the system prompt per run. Default: 16000 (~4 000 tokens).', zh:'單次執行注入 system prompt 的 project memory 字元數上限。預設 16000（約 4000 tokens）。'}, type:'number' },
  PIPELINE_PROJECT_PROFILE:          { label:'Project Profile Path',        desc:{en:'Path to a JSON project profile file that overrides pipeline defaults. Leave blank to auto-detect.', zh:'用來覆寫管線預設值的 JSON project profile 檔案路徑。留空使用自動偵測。'}, type:'text' },
  ENHANCED_BACKTEST_RUNNER:          { label:'Backtest Runner (default)',    desc:{en:'Run automated backtest pipeline (data prep, execution, param sweep, LLM fix loop) by default. Enable with --backtest-runner.', zh:'預設執行自動化回測管線（資料準備、執行、參數掃描、LLM 修復迴圈）。需搭配 --backtest-runner 開啟。'}, type:'boolean' },
  BACKTEST_REQUIRE_REAL_DATA:        { label:'Require Real Market Data',     desc:{en:'Refuse the synthetic-GBM fallback when no real data provider succeeds. With this on (default), the pipeline raises BacktestDataIntegrityError instead of silently producing meaningless Sharpe / drawdown numbers from random walks. Turn off only for offline CI smoke tests.', zh:'真實資料提供者都失敗時，拒絕回退到 synthetic-GBM 假資料。預設開啟：管線會丟出 BacktestDataIntegrityError，避免從隨機漫步產出毫無意義的 Sharpe / 回撤數字。只有離線 CI 煙霧測試時才關閉。'}, type:'boolean' },
  BACKTEST_MIN_REAL_DATA_ROWS:       { label:'Min Real-Data Rows',          desc:{en:'Minimum OHLCV rows a fetched dataset must contain before the runner accepts it. Defends against yfinance partial responses (1-2 stale rows from delisted tickers). Effective threshold is max(this value, 30% of the timeframe profile synthetic_rows). Default: 30.', zh:'抓回的 OHLCV 至少要有幾列才算可用，防止 yfinance 對下市標的回傳 1-2 列殘缺資料。實際門檻為 max(此值, 30% × 該週期 profile 的 synthetic_rows)。預設 30。'}, type:'number' },
  BACKTEST_DATA_CACHE_TTL_HOURS:     { label:'Data Cache TTL (hours)',      desc:{en:'How long to keep cached OHLCV CSV files on disk. Subsequent runs the same day reuse the cache, avoiding repeated API calls and rate-limit risk. 0 disables caching. Default: 24.', zh:'OHLCV CSV 快取的保存時間（小時）。同日後續執行會重用快取，避免重複呼叫 API 撞 rate limit。0 = 關閉快取。預設 24。'}, type:'number' },
  BACKTEST_DATA_CACHE_DIR:           { label:'Data Cache Directory',        desc:{en:'Directory for cached OHLCV CSV files. Leave blank to use ~/.crucible/data_cache. Supports ~ expansion.', zh:'OHLCV CSV 快取目錄。留空使用 ~/.crucible/data_cache。支援 ~ 展開。'}, type:'text' },
  BACKTEST_DATA_MAX_STALENESS_DAYS:  { label:'Max Data Staleness (days)',   desc:{en:'When the most recent data row is older than this many days, the report appends a warning that metrics may not reflect the current regime. 0 disables the check. Default: 7.', zh:'最後一根 K 線距今超過幾天時，回測報告會多一條警告：目前 Sharpe / 回撤可能不反映當前市場狀態。0 = 關閉檢查。預設 7。'}, type:'number' },
  BACKTEST_SYNTHETIC_SEED:           { label:'Synthetic Seed',              desc:{en:'Seed for the GBM synthetic-data fallback (only used when BACKTEST_REQUIRE_REAL_DATA=0). An integer means reproducible; "random" picks a fresh seed each run. Default: 42 (preserves v1.0.x behaviour).', zh:'合成 GBM 資料的種子（僅在 BACKTEST_REQUIRE_REAL_DATA=0 時生效）。整數表示可重現；"random" 表示每次重新抽。預設 42（保留 v1.0.x 行為）。'}, type:'text' },
  BACKTEST_PARAM_SEED:               { label:'Param Search Seed',           desc:{en:'Seed for the random-search parameter sweep RNG (used when optuna is unavailable, or when BACKTEST_PARAM_SEARCH=random). An integer makes best_params reproducible across re-runs of the same strategy; "random" uses an OS-time seed (legacy, not recommended — results drift). Default: 4242.', zh:'隨機參數搜尋 RNG 的種子（在 optuna 不可用、或 BACKTEST_PARAM_SEARCH=random 時使用）。整數可讓同一策略重跑時的 best_params 可重現；"random" 使用 OS 時間種子（舊行為，不建議——結果會漂移）。預設 4242。'}, type:'text' },
  BACKTEST_FETCH_HARD_TIMEOUT_SEC:   { label:'Fetch Hard Timeout (s)',      desc:{en:'Hard wall-clock timeout (seconds) for each yfinance / Binance parallel-fetch worker. Caps the damage from a stalled or slowloris-style endpoint; a hit surfaces as a fetch failure rather than a hung pipeline. Default: 90.', zh:'每個 yfinance / Binance 平行抓取 worker 的硬性 wall-clock 逾時（秒）。限制停滯或 slowloris 式端點造成的傷害；逾時會被當成抓取失敗，而非讓管線卡死。預設 90。'}, type:'number' },
  BACKTEST_PREPARE_DATA_ONLY:        { label:'Prepare-Data Only (Dry Run)', desc:{en:'When on, the backtest pipeline exits immediately after preparing data — no strategy execution, no parameter sweep. Useful for confirming the data-fetch path is wired up. Default: off.', zh:'開啟時回測管線在準備資料完成後立即結束，不執行策略也不做參數掃描。用來確認資料路徑是否接通。預設關閉。'}, type:'boolean' },
  BACKTEST_PARALLEL_FETCH:           { label:'Parallel Real-Data Fetch',    desc:{en:'When on, the auto cascade fires yfinance and Binance concurrently and uses whichever returns valid data first. Off by default — sequential cascade is friendlier to provider rate limits and easier to debug.', zh:'開啟時 auto cascade 同時打 yfinance + Binance，誰先回傳有效資料就用誰。預設關閉，sequential cascade 對 provider rate limit 更友善、也較易除錯。'}, type:'boolean' },
  ENHANCED_GITHUB_REPO:              { label:'GitHub Repo URL',             desc:{en:'GitHub repository URL to analyse and inject as research context. Enable with --github-repo.', zh:'要分析並注入研究 context 的 GitHub repository URL。需搭配 --github-repo 開啟。'}, type:'text' },
  GITHUB_ANALYZER_TIMEOUT:           { label:'GitHub Analyzer Timeout (s)', desc:{en:'HTTP request timeout in seconds for GitHub API calls during repo analysis. Default: 15.', zh:'repo 分析期間 GitHub API 呼叫的 HTTP 請求逾時秒數。預設 15。'}, type:'number' },
  GITHUB_ANALYZER_MAX_RETRIES:       { label:'GitHub Analyzer Retries',     desc:{en:'Max retry attempts on transient GitHub API errors (429/5xx). Default: 2.', zh:'GitHub API 暫時性錯誤（429/5xx）的最大重試次數。預設 2。'}, type:'number' },
  GITHUB_ANALYZER_CACHE_TTL:         { label:'GitHub Analyzer Cache TTL (s)', desc:{en:'Seconds to cache GitHub API responses (0 = disable cache). Default: 3600.', zh:'GitHub API 回應的快取秒數（0 = 停用快取）。預設 3600。'}, type:'number' },
  ENHANCED_INGEST_DOCS:              { label:'Document Ingestion (default)', desc:{en:'Inject local documents into the pipeline context by default. Enable with --ingest-docs.', zh:'預設將本機文件注入管線 context。需搭配 --ingest-docs 開啟。'}, type:'boolean' },
  ENHANCED_INGEST_DOCS_DIR:          { label:'Ingest Docs Directory',       desc:{en:'Directory of documents to ingest (PDF/MD/TXT/DOCX). Read by --ingest-docs.', zh:'要 ingest 的文件目錄（PDF/MD/TXT/DOCX）。由 --ingest-docs 讀取。'}, type:'text' },
  DOCUMENT_INGESTION_MAX_CHARS:      { label:'Max Chars per Document',      desc:{en:'Maximum characters read from a single document during ingestion. Default: 8000.', zh:'ingest 時單一文件讀取的最大字元數。預設 8000。'}, type:'number' },
  DOCUMENT_INGESTION_TOTAL_CHARS:    { label:'Total Ingestion Budget (chars)', desc:{en:'Maximum total characters across all ingested documents per run. Default: 24000.', zh:'單次執行所有 ingest 文件的總字元數上限。預設 24000。'}, type:'number' },
  ENHANCED_MULTILANG_CODEGEN:        { label:'Multi-Lang Codegen (default)', desc:{en:'Generate TypeScript/Go translations of Stage 4 Python output by default. Enable with --multilang-codegen.', zh:'預設為 Stage 4 Python 輸出產生 TypeScript / Go 翻譯。需搭配 --multilang-codegen 開啟。'}, type:'boolean' },
  ENHANCED_MULTILANG_LANGS:          { label:'Multi-Lang Target Languages', desc:{en:'Comma-separated target languages for multi-language codegen (default: typescript,go).', zh:'多語言 codegen 的目標語言，以逗號分隔（預設：typescript,go）。'}, type:'text' },
  MULTILANG_MAX_FILES:               { label:'Max Files to Translate',      desc:{en:'Maximum number of Python files sent to LLM for multi-language translation per run. Default: 10.', zh:'單次執行送 LLM 翻譯的 Python 檔數上限。預設 10。'}, type:'number' },
  MULTILANG_MAX_CHARS:               { label:'Max Chars per File',          desc:{en:'Maximum characters of source code sent per file to the LLM translator. Default: 4000.', zh:'每個檔案送 LLM 翻譯的原始碼字元數上限。預設 4000。'}, type:'number' },
  MULTILANG_ENABLE_RUST:             { label:'Enable Rust Target',          desc:{en:'Allow Rust 2021 edition as a translation target (experimental). Disabled by default.', zh:'允許 Rust 2021 edition 作為翻譯目標（實驗性功能）。預設停用。'}, type:'boolean' },
  NOTIFY_WEBHOOK_URL:                { label:'Custom Webhook URL',          desc:{en:'Generic webhook called on pipeline completion.', zh:'管線完成時呼叫的通用 webhook。'},             type:'password' },
  NOTIFY_SLACK_WEBHOOK_URL:          { label:'Slack Webhook URL',           desc:{en:'Slack incoming webhook URL.', zh:'Slack incoming webhook URL。'},                               type:'password' },
  NOTIFY_DISCORD_WEBHOOK_URL:        { label:'Discord Webhook URL',         desc:{en:'Discord incoming webhook URL.', zh:'Discord incoming webhook URL。'},                             type:'password' },
  NOTIFY_ON_FAIL_ONLY:               { label:'Notify on Failure Only',      desc:{en:'Skip notifications for successful runs.', zh:'成功的 run 略過通知。'},                   type:'boolean' },
  ALPHA_VANTAGE_API_KEY:             { label:'Alpha Vantage API Key',       desc:{en:'Required for alpha_vantage data source.', zh:'使用 alpha_vantage 資料來源時必填。'},                   type:'password' },
  ALPHA_VANTAGE_BASE_URL:            { label:'Alpha Vantage Base URL',      desc:{en:'Override only if using a proxy.', zh:'僅在使用代理時修改。'},                           type:'text' },
  FRED_API_KEY:                      { label:'FRED API Key',                desc:{en:'Federal Reserve Economic Data — optional, increases limits.', zh:'美國聯準會經濟資料 — 可選，提供後可提高請求上限。'},type:'password' },
  FRED_BASE_URL:                     { label:'FRED Base URL',               desc:{en:'Override only if using a proxy.', zh:'僅在使用代理時修改。'},                           type:'text' },
  COINGECKO_BASE_URL:                { label:'CoinGecko Base URL',          desc:{en:'Free tier requires no API key.', zh:'免費版不需 API key。'},                            type:'text' },
  EXTERNAL_DATA_TIMEOUT:             { label:'Request Timeout (s)',         desc:{en:'HTTP fetch timeout shared across all connectors.', zh:'所有 connector 共用的 HTTP 抓取逾時秒數。'},          type:'number' },
  EXTERNAL_DATA_MAX_RETRIES:         { label:'Max Retries',                 desc:{en:'Retry attempts for failed external data fetches.', zh:'外部資料抓取失敗時的重試次數。'},          type:'number' },
  AB_TEST_TIMEOUT:                   { label:'Test Timeout (s)',            desc:{en:'Max seconds per pipeline subprocess in an A/B run.', zh:'A/B 執行內每個管線 subprocess 的最大秒數。'},        type:'number' },
  AB_TEST_PARALLEL:                  { label:'Run in Parallel',             desc:{en:'Run both variants simultaneously (uses more resources).', zh:'兩個變體同時執行（會使用較多資源）。'},   type:'boolean' },
  BACKTEST_PARAM_SEARCH:             { label:'Param Search Strategy',       desc:{en:'Hyperparameter search method: grid, random, or bayesian (requires optuna).', zh:'超參數搜尋方法：grid（網格）、random（隨機）或 bayesian（貝氏，需安裝 optuna）。'}, type:'select', opts:[{v:'grid',l:'Grid'},{v:'random',l:'Random'},{v:'bayesian',l:'Bayesian (Optuna)'}] },
  BACKTEST_BAYESIAN_N_TRIALS:        { label:'Bayesian Trials',             desc:{en:'Number of Optuna TPE trials when using bayesian search.', zh:'使用貝氏搜尋時的 Optuna TPE 試驗次數。'},   type:'number' },
  BACKTEST_SYMBOL:                   { label:'Ticker Symbol',               desc:{en:'Primary ticker/symbol to download (e.g. SPY, BTC-USD). Default: SPY.', zh:'要下載的主要標的代碼（例如 SPY、BTC-USD）。預設 SPY。'}, type:'text' },
  BACKTEST_DATA_SOURCE:              { label:'Data Source',                 desc:{en:'Force a specific data source. "auto" lets the runner decide. Default: auto.', zh:'強制指定資料來源。"auto" 讓 runner 自行決定。預設 auto。'}, type:'select', opts:[{v:'auto',l:'Auto'},{v:'yfinance',l:'yfinance'},{v:'binance',l:'Binance'},{v:'project',l:'Project Files'}] },
  BACKTEST_PERIOD:                   { label:'Download Period',             desc:{en:'yfinance download period (e.g. 2y, 5y, max). "auto" derives from the strategy. Default: auto.', zh:'yfinance 下載期間（例如 2y、5y、max）。"auto" 會從策略自動推導。預設 auto。'}, type:'text' },
  BACKTEST_INTERVAL:                 { label:'Candle Interval',             desc:{en:'yfinance candle interval (e.g. 1d, 1h, 5m). "auto" derives from the strategy. Default: auto.', zh:'yfinance K 線間隔（例如 1d、1h、5m）。"auto" 會從策略自動推導。預設 auto。'}, type:'text' },
  BACKTEST_DATA_ROWS:                { label:'Synthetic Rows',              desc:{en:'Rows of synthetic OHLCV fallback data when live fetch fails. Default: 500.', zh:'即時下載失敗時，合成 OHLCV 備援資料的列數。預設 500。'}, type:'number' },
  BACKTEST_INITIAL_CAPITAL:          { label:'Initial Capital',             desc:{en:'Starting capital (USD) for synthetic / paper trading runs. Default: 100000.', zh:'合成 / 模擬交易執行的起始資金（USD）。預設 100000。'}, type:'number' },
  BACKTEST_MAX_COMBOS:               { label:'Max Combos',                  desc:{en:'Maximum parameter combinations to evaluate during hyperparameter search. Default: 50.', zh:'超參數搜尋時要評估的參數組合上限。預設 50。'}, type:'number' },
  BACKTEST_TARGET_METRIC:            { label:'Target Metric',               desc:{en:'Metric to optimise for during param search (e.g. sharpe_ratio, calmar_ratio, total_return). Default: sharpe_ratio.', zh:'參數搜尋時要最佳化的指標（例如 sharpe_ratio、calmar_ratio、total_return）。預設 sharpe_ratio。'}, type:'text' },
  BACKTEST_FIX_MAX_ROUNDS:           { label:'Fix Max Rounds',              desc:{en:'Max LLM auto-fix iterations if the backtest script fails. Default: 3.', zh:'回測腳本失敗時，LLM 自動修復的最大輪數。預設 3。'}, type:'number' },
  BACKTEST_TIMEOUT:                  { label:'Backtest Timeout (s)',        desc:{en:'Max seconds per backtest subprocess before it is killed. Default: 120.', zh:'單次回測 subprocess 被強制結束前的最大秒數。預設 120。'}, type:'number' },
  PORTFOLIO_REBALANCE_PERIOD:        { label:'Rebalance Period',            desc:{en:'Portfolio rebalancing frequency for combined equity curve.', zh:'合併淨值曲線的投組再平衡頻率。'}, type:'select', opts:[{v:'daily',l:'Daily'},{v:'weekly',l:'Weekly'},{v:'monthly',l:'Monthly'},{v:'quarterly',l:'Quarterly'},{v:'annual',l:'Annual'}] },
  PORTFOLIO_RISK_FREE_RATE:          { label:'Risk-Free Rate',              desc:{en:'Annualised risk-free rate for Sharpe/Sortino calculations (e.g. 0.04 = 4%).', zh:'用於 Sharpe / Sortino 計算的年化無風險利率（例如 0.04 = 4%）。'}, type:'number' },
  // Quant Analytics Suite — feature enable flags
  ENHANCED_QUANT_ANALYTICS:      { label:'Quant Analytics (default)',  desc:{en:'Run Walk-Forward + Significance Testing after a Quant mode backtest by default. Enable with --quant-analytics.', zh:'預設在 Quant 模式回測後執行 Walk-Forward 與顯著性檢定。需搭配 --quant-analytics 開啟。'}, type:'boolean' },
  ENHANCED_WALK_FORWARD:         { label:'Walk-Forward (default)',      desc:{en:'Enable walk-forward validation within --quant-analytics (default: on when analytics enabled).', zh:'在 --quant-analytics 內啟用 walk-forward 驗證（analytics 開啟時預設為 on）。'}, type:'boolean' },
  ENHANCED_SIGNIFICANCE_TEST:    { label:'Significance Test (default)', desc:{en:'Enable permutation/bootstrap significance test within --quant-analytics (default: on).', zh:'在 --quant-analytics 內啟用 permutation / bootstrap 顯著性檢定（預設 on）。'}, type:'boolean' },
  ENHANCED_REGIME_DETECTION:     { label:'Regime Detection (default)',  desc:{en:'Detect market regimes (bull/bear/sideways) from backtest price data by default. Enable with --regime-detection.', zh:'預設從回測價格資料偵測市場狀態（多頭 / 空頭 / 盤整）。需搭配 --regime-detection 開啟。'}, type:'boolean' },
  ENHANCED_FACTOR_ANALYSIS:      { label:'Factor Analysis (default)',   desc:{en:'Run CAPM/Fama-French factor exposure regression by default. Enable with --factor-analysis.', zh:'預設執行 CAPM / Fama-French 因子曝險迴歸。需搭配 --factor-analysis 開啟。'}, type:'boolean' },
  ENHANCED_TRANSACTION_COST:     { label:'Transaction Cost (default)',  desc:{en:'Run transaction cost sensitivity analysis by default. Enable with --transaction-cost.', zh:'預設執行交易成本敏感度分析。需搭配 --transaction-cost 開啟。'}, type:'boolean' },
  ENHANCED_MONTE_CARLO:          { label:'Monte Carlo (default)',       desc:{en:'Run Monte Carlo simulation and stress tests by default. Enable with --monte-carlo.', zh:'預設執行 Monte Carlo 模擬與壓力測試。需搭配 --monte-carlo 開啟。'}, type:'boolean' },
  ENHANCED_TEARSHEET:            { label:'Tearsheet (default)',         desc:{en:'Generate rich Markdown strategy tearsheet by default. Enable with --tearsheet.', zh:'預設產生完整 Markdown 策略 tearsheet。需搭配 --tearsheet 開啟。'}, type:'boolean' },
  ENHANCED_SIGNAL_ANALYSIS:      { label:'Signal Analysis (default)',   desc:{en:'Run signal decay analysis to measure edge half-life by default. Enable with --signal-analysis.', zh:'預設執行訊號衰減分析，衡量 edge 半衰期。需搭配 --signal-analysis 開啟。'}, type:'boolean' },
  ENHANCED_COINTEGRATION:        { label:'Cointegration (default)',     desc:{en:'Run cointegration + pairs trading analysis on multi-asset data by default. Enable with --cointegration.', zh:'預設對多資產資料執行共整合 + 配對交易分析。需搭配 --cointegration 開啟。'}, type:'boolean' },
  ENHANCED_DYNAMIC_CORRELATION:  { label:'Dynamic Correlation (default)', desc:{en:'Compute rolling correlation matrix and PCA decomposition by default. Enable with --dynamic-correlation.', zh:'預設計算滾動相關係數矩陣與 PCA 分解。需搭配 --dynamic-correlation 開啟。'}, type:'boolean' },
  // Quant Analytics Suite env vars
  WALK_FORWARD_N_SPLITS:         { label:'WF Splits',               desc:{en:'Number of IS/OOS rolling splits for walk-forward validation. Default: 5.', zh:'walk-forward 驗證的 IS / OOS 滾動切割數。預設 5。'},            type:'number' },
  WALK_FORWARD_OOS_PCT:          { label:'WF OOS %',                desc:{en:'Fraction of each split used as out-of-sample (0.0–1.0). Default: 0.3.', zh:'每個切割中作為樣本外的比例（0.0–1.0）。預設 0.3。'},              type:'number' },
  WALK_FORWARD_IS_PCT:           { label:'WF Use % Splits',         desc:{en:'Use percentage-based IS/OOS splits (true) vs fixed-bar-count splits (false). Default: true.', zh:'使用百分比切割（true）或固定 bar 數切割（false）。預設 true。'}, type:'boolean' },
  WALK_FORWARD_MIN_TRAIN_BARS:   { label:'WF Min Train Bars',       desc:{en:'Minimum number of in-sample bars required per fold; folds below this are skipped. Default: 100.', zh:'每個 fold 所需的最少樣本內 bar 數，少於此數的 fold 會被跳過。預設 100。'}, type:'number' },
  SIG_N_PERMUTATIONS:            { label:'Permutations',            desc:{en:'Number of random permutations for the significance p-value estimate. Default: 1000.', zh:'估算顯著性 p-value 的隨機重排次數。預設 1000。'},  type:'number' },
  SIG_N_BOOTSTRAP:               { label:'Signal Bootstrap N',      desc:{en:'Bootstrap resamples for signal confidence-interval construction. Default: 1000.', zh:'建構訊號信賴區間的 bootstrap 重抽樣次數。預設 1000。'},      type:'number' },
  SIG_CONFIDENCE_LEVEL:          { label:'Signal CI Level',         desc:{en:'Confidence level for signal bootstrap CIs (e.g. 0.95 = 95% CI). Default: 0.95.', zh:'訊號 bootstrap 信賴區間的信心水準（例如 0.95 = 95% CI）。預設 0.95。'},      type:'number' },
  REGIME_METHOD:                 { label:'Regime Method',           desc:{en:'Default regime detection algorithm: volatility, trend, or hmm. Default: volatility.', zh:'預設的市場狀態偵測演算法：volatility（波動率）、trend（趨勢）或 hmm。預設 volatility。'},  type:'select', opts:[{v:'volatility',l:'Volatility Threshold'},{v:'trend',l:'SMA Trend Band'},{v:'hmm',l:'Baum-Welch HMM'}] },
  REGIME_N_REGIMES:              { label:'HMM Regimes',             desc:{en:'Number of hidden states in the Baum-Welch HMM model. Default: 3.', zh:'Baum-Welch HMM 模型的隱狀態數。預設 3。'},                    type:'number' },
  REGIME_VOL_WINDOW:             { label:'Vol Window (bars)',        desc:{en:'Rolling window for volatility-threshold regime detection. Default: 20.', zh:'波動率閾值法狀態偵測的滾動窗口（bar 數）。預設 20。'},               type:'number' },
  REGIME_TREND_WINDOW:           { label:'Trend Window (bars)',      desc:{en:'Rolling window for SMA trend-band regime detection. Default: 50.', zh:'SMA 趨勢帶狀態偵測的滾動窗口（bar 數）。預設 50。'},                    type:'number' },
  REGIME_LOOKBACK_BARS:          { label:'Regime Lookback (bars)',   desc:{en:'Limit regime detection to last N bars (0 = use all available data). Default: 0.', zh:'限制狀態偵測只看最近 N 個 bar（0 = 使用全部可用資料）。預設 0。'},     type:'number' },
  MC_N_SIMULATIONS:              { label:'MC Paths',                desc:{en:'Number of Monte Carlo bootstrap simulation paths. Default: 5000.', zh:'Monte Carlo bootstrap 模擬路徑數。預設 5000。'},                    type:'number' },
  MC_HORIZON_DAYS:               { label:'MC Horizon (days)',       desc:{en:'Number of trading days to simulate forward in Monte Carlo. Default: 252.', zh:'Monte Carlo 向前模擬的交易日數。預設 252。'},             type:'number' },
  MC_METHOD:                     { label:'MC Method',               desc:{en:'Monte Carlo simulation method. Default: bootstrap (block-resample from actual returns).', zh:'Monte Carlo 模擬方法。預設 bootstrap（從實際收益做 block 重抽樣）。'}, type:'select', opts:[{v:'bootstrap',l:'Bootstrap (block-resample)'}] },
  MC_SEED:                       { label:'MC Random Seed',          desc:{en:'Random seed for Monte Carlo reproducibility (-1 = random each run). Default: 42.', zh:'Monte Carlo 可重現性的隨機種子（-1 = 每次執行隨機）。預設 42。'},    type:'number' },
  FACTOR_RISK_FREE_RATE:         { label:'Factor RF Rate',          desc:{en:'Annualised risk-free rate for CAPM alpha computation (e.g. 0.04). Default: 0.04.', zh:'計算 CAPM alpha 用的年化無風險利率（例如 0.04）。預設 0.04。'},    type:'number' },
  FACTOR_LOOKBACK_DAYS:          { label:'Factor Lookback (days)',  desc:{en:'Number of trading days used for factor regression. Default: 252.', zh:'因子迴歸使用的交易日數。預設 252。'},                    type:'number' },
  FACTOR_USE_FF_DATA:            { label:'Use Fama-French Data',    desc:{en:'Download Fama-French factor data for 3-factor/5-factor regression (requires internet). Default: false.', zh:'下載 Fama-French 因子資料以做 3 因子 / 5 因子迴歸（需要網路）。預設 false。'}, type:'boolean' },
  SIGNAL_HORIZONS:               { label:'Signal Horizons',         desc:{en:'Comma-separated forward-return horizons in days (e.g. 1,2,5,10,20). Default: 1,2,3,5,10,20,40.', zh:'前向收益的觀察期（天），以逗號分隔（例如 1,2,5,10,20）。預設 1,2,3,5,10,20,40。'}, type:'text' },
  SIGNAL_MIN_OBSERVATIONS:       { label:'Signal Min Obs',          desc:{en:'Minimum number of observations required per horizon for t-stat. Default: 30.', zh:'每個 horizon 計算 t-stat 所需的最少觀察數。預設 30。'},        type:'number' },
  SIGNAL_SIGNIFICANCE_THRESH:    { label:'Signal Sig Threshold',    desc:{en:'p-value threshold for marking a horizon as statistically significant. Default: 0.05.', zh:'判定 horizon 是否統計顯著的 p-value 閾值。預設 0.05。'}, type:'number' },
  RISK_METHOD:                   { label:'Risk Method',             desc:{en:'Risk attribution computation method. Default: historical (empirical percentile VaR/CVaR).', zh:'風險歸因計算方法。預設 historical（經驗百分位 VaR / CVaR）。'}, type:'select', opts:[{v:'historical',l:'Historical (empirical)'},{v:'parametric',l:'Parametric (normal)'},{v:'ewma',l:'EWMA (exp. weighted)'}] },
  RISK_CONFIDENCE_LEVEL:         { label:'VaR Confidence',          desc:{en:'Confidence level for VaR/CVaR calculations (e.g. 0.95 = 95%). Default: 0.95.', zh:'VaR / CVaR 計算的信心水準（例如 0.95 = 95%）。預設 0.95。'},        type:'number' },
  RISK_LOOKBACK_WINDOW:          { label:'Risk Lookback (bars)',     desc:{en:'Rolling window in bars for risk calculations. Default: 252 (1 trading year).', zh:'風險計算的滾動窗口（bar 數）。預設 252（一個交易年）。'},         type:'number' },
  ENHANCED_RISK_ATTRIBUTION:     { label:'Risk Attribution (default)', desc:{en:'Enable --risk-attribution by default on every run without passing the flag explicitly.', zh:'預設啟用 --risk-attribution，不需每次明確帶旗標。'}, type:'boolean' },
  TC_COMMISSION_PCT:             { label:'Commission (%)',           desc:{en:'Commission per trade as a decimal fraction (e.g. 0.001 = 0.1% = 10 bps). Default: 0.001.', zh:'每筆交易的佣金（小數，例如 0.001 = 0.1% = 10 bps）。預設 0.001。'}, type:'number' },
  TC_SLIPPAGE_PCT:               { label:'Slippage (%)',             desc:{en:'Slippage per trade as a decimal fraction (e.g. 0.0005 = 0.05% = 5 bps). Default: 0.0005.', zh:'每筆交易的滑價（小數，例如 0.0005 = 0.05% = 5 bps）。預設 0.0005。'}, type:'number' },
  TC_SPREAD_BPS:                 { label:'Spread (bps)',             desc:{en:'Bid-ask half-spread in basis points applied to each fill. Default: 2.0.', zh:'套用到每筆成交的買賣價差半幅（bps）。預設 2.0。'},             type:'number' },
  TC_USE_KYLE_IMPACT:            { label:'Kyle Market Impact',       desc:{en:'Enable non-linear market impact modelling via the Kyle-lambda formula. Default: false.', zh:'啟用 Kyle-lambda 公式做非線性市場衝擊建模。預設 false。'}, type:'boolean' },
  TC_KYLE_LAMBDA:                { label:'Kyle Lambda',              desc:{en:'Kyle-lambda market impact coefficient (higher = more impact per unit of volume). Default: 0.1.', zh:'Kyle-lambda 市場衝擊係數（越高 = 每單位 volume 的衝擊越大）。預設 0.1。'}, type:'number' },
  TC_AVG_DAILY_VOLUME:           { label:'Avg Daily Volume',         desc:{en:'Average daily volume for impact scaling (0 = use strategy default / disable). Default: 0.', zh:'用於衝擊縮放的日均 volume（0 = 使用策略預設 / 停用）。預設 0。'}, type:'number' },
  TC_N_SCENARIOS:                { label:'TC Scenarios',             desc:{en:'Monte Carlo scenarios for transaction cost sensitivity analysis. Default: 10.', zh:'交易成本敏感度分析的 Monte Carlo 情境數。預設 10。'},        type:'number' },
  TEARSHEET_MONTHLY_RETURNS:     { label:'Monthly Returns',          desc:{en:'Include monthly returns heatmap table in the tearsheet output. Default: true.', zh:'tearsheet 輸出包含月度收益熱圖表。預設 true。'},        type:'boolean' },
  TEARSHEET_DRAWDOWN_PERIODS:    { label:'Drawdown Periods',         desc:{en:'Include top drawdown periods table in the tearsheet output. Default: true.', zh:'tearsheet 輸出包含最大回撤期間表。預設 true。'},           type:'boolean' },
  TEARSHEET_MAX_DRAWDOWN_PERIODS:{ label:'Max Drawdown Rows',        desc:{en:'Number of worst drawdown periods to list in the table. Default: 5.', zh:'表中列出的最差回撤期間數量。預設 5。'},                  type:'number' },
  TEARSHEET_TRADE_ANALYSIS:      { label:'Trade Analysis',           desc:{en:'Include per-trade statistics (win rate, avg win/loss, profit factor) in the tearsheet. Default: true.', zh:'tearsheet 包含每筆交易統計（勝率、平均盈虧、profit factor）。預設 true。'}, type:'boolean' },
  MLFLOW_TRACKING_URI:               { label:'Tracking URI',                desc:{en:'MLflow server URI. When set, every run is logged as an MLflow experiment.', zh:'MLflow 伺服器 URI。設定後每次執行都會記錄為 MLflow 實驗。'}, type:'text' },
  MLFLOW_EXPERIMENT_NAME:            { label:'Experiment Name',             desc:{en:'MLflow experiment name (default: Crucible).', zh:'MLflow 實驗名稱（預設：Crucible）。'},          type:'text' },
  MLFLOW_LOG_ARTIFACTS:              { label:'Log Artifacts',               desc:{en:'Upload the HTML report as an MLflow artifact on completion.', zh:'執行結束時將 HTML 報告作為 MLflow artifact 上傳。'}, type:'boolean' },
  WEBHOOK_SECRET:                    { label:'HMAC Secret',                 desc:{en:'HMAC-SHA256 secret for POST /webhook/trigger signature validation. Leave blank to disable signature checks.', zh:'POST /webhook/trigger 簽章驗證用的 HMAC-SHA256 secret。留空停用簽章檢查。'}, type:'password' },
  CRUCIBLE_RUN_INSIGHTS_ENABLED:          { label:'Enable Ledger',         desc:{en:'Master switch for the run-insights ledger. Set to false to disable the entire subsystem — recorder becomes a no-op, zero I/O, and the per-run Insights tab + dashboard widget show as disabled.', zh:'Run Insights 帳本主開關。關閉後整個子系統停用:recorder 變 no-op、零 I/O、per-run Insights 分頁與 dashboard 卡片顯示停用。'}, type:'boolean' },
  CRUCIBLE_RUN_INSIGHTS_RECORD_OUTPUT:    { label:'Record Output Methods', desc:{en:'Record an output_method event each time section 07 successfully saves a project (model id, framework, validation verdict, artefact list, score).', zh:'section 07 成功儲存專案時記錄 output_method 事件(模型 id、framework、驗證結論、artefact 清單、score)。'}, type:'boolean' },
  CRUCIBLE_RUN_INSIGHTS_RECORD_ERRORS:    { label:'Record Errors',         desc:{en:'Record an error_record event when a retryable operation exhausts all retries in resilience.kickoff_crew_with_retry (exception class, message head, retry count).', zh:'resilience.kickoff_crew_with_retry 重試耗盡時記錄 error_record 事件(例外類別、message head、retry 次數)。'}, type:'boolean' },
  CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE:    { label:'Record Debate Rejections', desc:{en:'Record a direction_debate_rejection event when Stage 0 force-nones a candidate direction, or parse-fails after all fallbacks (judge verdict excerpt, rejection reason).', zh:'Stage 0 force-none 候選方向或所有 fallback 後仍 parse 失敗時記錄 direction_debate_rejection 事件(judge 評語節錄、拒絕原因)。'}, type:'boolean' },
  CRUCIBLE_RUN_INSIGHTS_RECORD_PARAMS:    { label:'Record Runtime Parameters', desc:{en:'auto = record in Quant mode only (matches the Quant-records / non-Quant-skips requirement); 1 = always record; 0 = never record. Typos fall back to auto, never truthy-coerce.', zh:'auto = 僅 Quant 模式記錄(符合「Quant 記錄、非 Quant 不記錄」需求);1 = 永遠記錄;0 = 永遠不記錄。拼錯的值 fallback 回 auto,不會被 truthy-coerce。'}, type:'select', opts:[{v:'auto',l:'auto (Quant only)'},{v:'1',l:'1 (always record)'},{v:'0',l:'0 (never record)'}] },
  CRUCIBLE_RUN_INSIGHTS_REDACT:           { label:'Redact Sensitive Fields', desc:{en:'Recursively redact sensitive field names (api_key, token, secret, webhook_url, auth, etc.) from payloads before write. Highly recommended; runtime_params payloads can embed operator config dicts with webhook URLs / API tokens.', zh:'寫入前對敏感欄位(api_key、token、secret、webhook_url、auth 等)做遞迴遮蔽。強烈建議開啟;runtime_params payload 可能嵌入含 webhook URL / API token 的設定 dict。'}, type:'boolean' },
  CRUCIBLE_RUN_INSIGHTS_BACKEND:          { label:'Storage Backend',       desc:{en:'Storage backend selector. local = JSONL streams under .crucible_insights/ (only this is implemented today). cloudflare / dual are protocol stubs that raise NotImplementedError until the cloud backend ships.', zh:'儲存後端選擇器。local = .crucible_insights/ 下的 JSONL streams(目前唯一實作);cloudflare / dual 為 protocol stub,雲端後端上線前會 raise NotImplementedError。'}, type:'select', opts:[{v:'local',l:'local (JSONL on disk)'},{v:'cloudflare',l:'cloudflare (not yet implemented)'},{v:'dual',l:'dual (not yet implemented)'}] },

  // v1.1.8 — Direction Debate Audit Mode keys.  All descriptions are
  // bilingual ({en, zh}) per the v1.1.0 KEY_META bilingual contract.  The
  // ISOLATION_MODE select offers two opts; RECORD_DEBATE_FINDING offers
  // auto/1/0 to match the runtime_params pattern.
  CRUCIBLE_DEBATE_AUDIT_MODE: { label:'Enable Audit Mode', desc:{en:'Master switch for Direction Debate Audit Mode. When enabled, every specialist (Explorer, Comparator, Skeptic, Evidence Auditor, Judge) emits a structured AUDIT_FINDING block and the Judge emits a GATE_VERDICT in the expanded PROCEED/BRANCH/KILL/NEEDS_MORE_DATA space. v1.1.8 is observation-only: the audit ledger captures the disagreement trace but legacy force-none flow is unchanged for back-compat.', zh:'Direction Debate Audit Mode 主開關。開啟後每位 specialist (Explorer / Comparator / Skeptic / Evidence Auditor / Judge) 都會輸出結構化的 AUDIT_FINDING 區塊，Judge 額外輸出 GATE_VERDICT (擴張的 PROCEED / BRANCH / KILL / NEEDS_MORE_DATA 決策空間)。v1.1.8 設計為「只觀察不覆寫」：audit ledger 抓到分歧軌跡，但舊的 force-none 行為保持不變以維持 back-compat。'}, type:'boolean' },
  CRUCIBLE_DEBATE_REQUIRE_STRUCTURED_FINDINGS: { label:'Require Structured Findings', desc:{en:'When enabled, the orchestrator retries direction-debate attempts where any specialist failed to emit a parseable AUDIT_FINDING block. When disabled, missing findings are silently skipped (audit trail will be sparse but the main pipeline proceeds).', zh:'開啟時，若任一 specialist 未輸出可解析的 AUDIT_FINDING 區塊，orchestrator 會重試 direction-debate。關閉時，缺失的 findings 會被靜默跳過 (audit trail 會稀疏，但主流程繼續進行)。'}, type:'boolean' },
  CRUCIBLE_DEBATE_ISOLATION_MODE: { label:'Isolation Mode', desc:{en:'How prior agents’ context is shared with downstream agents. sequential (default) = full prior task output passed via Task.context (legacy v1.1.7 behaviour). hybrid = prior agents’ free-form chain-of-thought is marked untrusted; only their structured AUDIT_FINDING blocks are authoritative. hybrid reduces sequential anchoring at the cost of slightly stricter prompts.', zh:'前一位 agent 的 context 如何傳給下一位 agent。sequential (預設) = 完整輸出透過 Task.context 傳遞 (v1.1.7 既有行為)。hybrid = 前一位 agent 的 free-form 推理被標記為不可信，只有結構化的 AUDIT_FINDING 區塊是權威。hybrid 模式以稍嚴的 prompt 為代價，降低 sequential anchoring 風險。'}, type:'select', opts:[{v:'sequential',l:'sequential (legacy v1.1.7)'},{v:'hybrid',l:'hybrid (structured-only authority)'}] },
  CRUCIBLE_DEBATE_EXTERNAL_CRITIC: { label:'Enable External Critic', desc:{en:'Spawn a sixth agent that re-judges the Judge’s verdict using ONLY the raw research evidence + Judge’s decision token. Critic does NOT see prior agents’ chain-of-thought, so it is isolated from sequential anchoring. v1.1.8 uses the same model family as Judge (cross-family critic ships in v1.3.0).', zh:'啟用第六位 agent，僅使用「原始研究證據 + Judge 的決策 token」重新審判 Judge 的結論。Critic 看不到其他 agent 的推理過程，因此免於 sequential anchoring 偏差。v1.1.8 使用與 Judge 相同的模型族 (跨模型族 critic 留待 v1.3.0)。'}, type:'boolean' },
  CRUCIBLE_DEBATE_CRITIC_OVERRIDE_PROCEED: { label:'Critic Can Override Judge', desc:{en:'When enabled, an External Critic KILL verdict overrides Judge PROCEED. When disabled (default), Critic dissent is recorded in audit_trail but the Judge verdict stands. Recommended to keep disabled until the Critic has been calibrated on real workloads.', zh:'開啟時，External Critic 的 KILL 結論可覆寫 Judge 的 PROCEED。關閉時 (預設)，Critic 的反對意見會寫入 audit_trail，但最終以 Judge 結論為準。建議在 Critic 校準完成前保持關閉。'}, type:'boolean' },
  CRUCIBLE_DEBATE_CONSENSUS_RISK_THRESHOLD: { label:'Consensus Risk Threshold', desc:{en:'Pairwise Jaccard distance threshold for the consensus-risk computation. When concern_diversity falls below this value AND mean confidence > 0.85, the low_diversity_high_confidence flag fires. Range [0.0, 1.0]; default 0.3. Lower = stricter (more sensitive to groupthink).', zh:'consensus-risk 計算的 pairwise Jaccard distance 閾值。當 concern_diversity 低於此值且平均 confidence > 0.85 時，會觸發 low_diversity_high_confidence 旗標。範圍 [0.0, 1.0]，預設 0.3。值越低越嚴格 (對 groupthink 更敏感)。'}, type:'number' },
  CRUCIBLE_DEBATE_CRITIC_MAX_ATTEMPTS: { label:'Critic Max Attempts', desc:{en:'Maximum retry attempts for the External Critic LLM call. Range 1-5; default 2. Hitting the limit returns a NEEDS_MORE_DATA fallback (never KILL) so a flaky LLM cannot silently destroy viable directions.', zh:'External Critic LLM 呼叫的最大重試次數。範圍 1-5，預設 2。耗盡重試後回傳 NEEDS_MORE_DATA 的安全 fallback (絕對不 KILL)，確保 LLM 不穩定時不會誤殺可行方向。'}, type:'number' },
  CRUCIBLE_RUN_INSIGHTS_RECORD_DEBATE_FINDING: { label:'Record Debate Findings', desc:{en:'auto = follow CRUCIBLE_DEBATE_AUDIT_MODE (record only in audit mode; default). 1 = always record findings (even when audit_mode is off). 0 = never record. Typos fall back to auto, never truthy-coerce.', zh:'auto = 跟隨 CRUCIBLE_DEBATE_AUDIT_MODE (只在 audit mode 開啟時記錄;預設)。1 = 永遠記錄 findings (即使 audit_mode 關閉)。0 = 永遠不記錄。拼錯的值 fallback 回 auto,不會被 truthy-coerce。'}, type:'select', opts:[{v:'auto',l:'auto (follow audit_mode)'},{v:'1',l:'1 (always record)'},{v:'0',l:'0 (never record)'}] },
  CRUCIBLE_RUN_INSIGHTS_RECORD_GATE_VERDICT: { label:'Record Gate Verdicts', desc:{en:'When enabled (default), every direction-debate attempt emits a debate_verdict ledger event with the Judge’s terminal decision (PROCEED / BRANCH / KILL / NEEDS_MORE_DATA). Gate verdicts are cheap to record and the single most-valuable signal for v1.2.0 retrieval — recommended to keep enabled.', zh:'開啟時 (預設)，每次 direction-debate 嘗試都會輸出一筆 debate_verdict ledger 事件，記錄 Judge 的終態決策 (PROCEED / BRANCH / KILL / NEEDS_MORE_DATA)。Gate verdicts 寫入成本低且對 v1.2.0 retrieval 是最有價值的訊號，建議保持開啟。'}, type:'boolean' },

  // v1.1.8 extended — Web Research Hardening: Search Cache (Q1).  All
  // descriptions bilingual {en, zh} per the v1.1.0 KEY_META contract.
  LIBRARIAN_SEARCH_DISK_CACHE_ENABLED: { label:'Disk Cache', desc:{en:'Disk-persistent cache for search-provider responses. When enabled, refinement iterations hit cache instead of re-fetching from the provider — typical repeat-run HTTP cost drops 80%+. Cache lives at LIBRARIAN_SEARCH_CACHE_PATH and is separate from the LLM cache. Disable only for cache debugging.', zh:'搜索 provider 回應的 disk 持久化 cache。開啟時 refinement 迭代命中 cache，不再重新打 provider — 重複跑同主題的 HTTP cost 降 80%+。Cache 檔位於 LIBRARIAN_SEARCH_CACHE_PATH，與 LLM cache 分離。除非要 debug cache 否則不要關。'}, type:'boolean' },
  LIBRARIAN_SEARCH_CACHE_PATH: { label:'Cache Path', desc:{en:'Repo-relative or absolute path for the SQLite search-cache file. Parent directory is auto-created. Override to share cache across multiple Crucible installations.', zh:'SQLite 搜索 cache 檔的 repo 相對或絕對路徑。父目錄自動建立。需要在多個 Crucible 安裝間共用 cache 時改這裡。'}, type:'text' },
  LIBRARIAN_SEARCH_CACHE_TTL_DDG_HOURS: { label:'DDG TTL (hours)', desc:{en:'DuckDuckGo cache TTL in hours. News drifts fast; 12 is a safe default. Tune down for breaking-news topics, up to reuse cache aggressively.', zh:'DuckDuckGo cache TTL（小時）。新聞 drift 快，預設 12 已偏保守。突發新聞主題調低，靜態主題可調高加大 cache 重用。'}, type:'number' },
  LIBRARIAN_SEARCH_CACHE_TTL_GITHUB_HOURS: { label:'GitHub TTL (hours)', desc:{en:'GitHub search cache TTL. Repo metadata changes slowly; 24h default. Increase for less-active repos.', zh:'GitHub 搜索 cache TTL。Repo metadata 變動慢，預設 24 小時。不活躍 repo 可調更長。'}, type:'number' },
  LIBRARIAN_SEARCH_CACHE_TTL_ARXIV_HOURS: { label:'arXiv TTL (hours)', desc:{en:'arXiv API cache TTL. Papers essentially never change once published; 168 hours (1 week) default.', zh:'arXiv API cache TTL。論文發表後幾乎不會改，預設 168 小時（1 週）。'}, type:'number' },
  LIBRARIAN_SEARCH_CACHE_TTL_CONTEXT7_HOURS: { label:'Context7 TTL (hours)', desc:{en:'Context7 cache TTL. context7 results depend on lane / user_problem context, so TTL is shorter than other providers; default 6 hours.', zh:'Context7 cache TTL。Context7 結果隨 lane / user_problem context 變動，TTL 較短，預設 6 小時。'}, type:'number' },

  // v1.1.8 extended — Web Research Hardening: Provider Resilience (Q2/Q3/Q5/Q6/Q7/Q9).
  LIBRARIAN_PROVIDER_COOLDOWN_INITIAL_SECONDS: { label:'Cooldown Initial (s)', desc:{en:'Initial cooldown when a provider returns 429 / 202. Doubles on each subsequent trigger up to Cooldown Max. During cooldown the provider is skipped; the fallback chain routes to the next-priority provider.', zh:'provider 收到 429 / 202 時的初始 cooldown 秒數。每次再觸發雙倍，上限 Cooldown Max。Cooldown 期間該 provider 直接跳過，fallback chain 自動路由到下一順位 provider。'}, type:'number' },
  LIBRARIAN_PROVIDER_COOLDOWN_MAX_SECONDS: { label:'Cooldown Max (s)', desc:{en:'Maximum cooldown duration per provider (doubling cap). Default 1800 = 30 minutes — beyond this the provider is presumed broken for the remainder of the run.', zh:'每個 provider 的 cooldown 上限。預設 1800 = 30 分鐘 — 超過代表該 provider 本次 run 基本壞掉，直接放棄。'}, type:'number' },
  LIBRARIAN_PROVIDER_FALLBACK_ENABLED: { label:'Fallback Chain', desc:{en:'Enable per-query-class fallback chain. When a primary provider returns empty or enters cooldown, the dispatcher auto-routes to the next provider in the same class (general web / code / academic / docs). Disable to revert to v1.1.7 silo behaviour.', zh:'啟用 per-query-class fallback chain。Primary provider 回空或進 cooldown 時，dispatcher 自動路由到同 class（general web / code / academic / docs）的下一順位 provider。關閉退回 v1.1.7 silo 行為。'}, type:'boolean' },
  LIBRARIAN_ASYNC_FANOUT_ENABLED: { label:'Async Fan-out', desc:{en:'Async provider fan-out using asyncio + per-provider Semaphore. Largest single librarian wall-clock win — typical 60%+ reduction. Disable to fall back to sequential dispatch (legacy v1.1.7) for emergency rollback.', zh:'用 asyncio + per-provider Semaphore 做 provider 並行 fan-out。Librarian wall-clock 單一最大優化（典型降 60%+）。緊急 rollback 可關閉退回 sequential（v1.1.7 行為）。'}, type:'boolean' },
  LIBRARIAN_CROSS_PROVIDER_DEDUP_ENABLED: { label:'Cross-Provider Dedup', desc:{en:'Cross-provider query deduplication. Same normalised query sent to multiple providers in the same query class is sent only once, followed by fallback if first returns empty. Saves ~30% of HTTP calls in typical runs.', zh:'跨 provider 的 query 去重。同一 normalized query 對同 class 內多 provider 只打第一個，回空才走 fallback。典型省 30% HTTP call。'}, type:'boolean' },
  LIBRARIAN_PROVIDER_HEALTH_SUMMARY: { label:'Health Summary', desc:{en:'Emit per-provider health summary at end of librarian stage: request count, 200 OK count, 429/202 count, timeout count, citation yield. Also written to .crucible_insights/output.jsonl for v1.2.0 retrieval.', zh:'librarian 階段結束時印出 per-provider 健康摘要：請求數、200 OK 數、429/202 數、timeout 數、citation 命中數。同時寫入 .crucible_insights/output.jsonl 供 v1.2.0 retrieval 使用。'}, type:'boolean' },
  LIBRARIAN_HTTP2_ENABLED: { label:'HTTP/2', desc:{en:'Enable HTTP/2 for outbound provider calls. Reduces per-request connection-setup cost by 10-20%. Disable only if a self-hosted SearXNG instance throws TLS handshake errors.', zh:'對 outbound provider 呼叫啟用 HTTP/2。Per-request 連線建立成本降 10-20%。只有自架 SearXNG 出 TLS handshake 錯誤時才需要關。'}, type:'boolean' },
  LIBRARIAN_HTTP_KEEPALIVE_ENABLED: { label:'HTTP Keep-Alive', desc:{en:'Enable connection-pool reuse across provider calls. Mostly a free win — disable only for HTTP debugging.', zh:'啟用 connection pool 跨 provider 呼叫重用。基本是免費效能優化 — 只有要 debug HTTP 才關。'}, type:'boolean' },

  // v1.1.8 extended — Web Research Hardening: Extra Providers (Q4).
  LIBRARIAN_EXTRA_PROVIDERS: { label:'Extra Providers', desc:{en:'Comma-separated list of zero-auth providers added to the core list (websearch, context7, grep_app, github, arxiv, paperswithcode). Default: openalex,crossref,wikipedia. Add ``searxng`` only after confirming a public instance you trust. Empty string disables extras.', zh:'用逗號分隔的免認證額外 provider 清單，加在 core list（websearch, context7, grep_app, github, arxiv, paperswithcode）之上。預設 openalex,crossref,wikipedia。確認過信任的公開 instance 才加 searxng。空字串表示停用 extras。'}, type:'text' },

  // v1.1.8 extended — Web Research Hardening: Query Quality (Q8/Q10/P2).
  LIBRARIAN_DOMAIN_PINS_ENABLED: { label:'Domain Pins', desc:{en:'Enable domain authoritative-source pinning. When user_problem matches a pin in LIBRARIAN_DOMAIN_PINS_PATH, the listed URLs are pre-fetched as Tier-1 anchors BEFORE search dispatch. Closes the gap where DDG returns Tier-2/3 transcriptions of an event but misses the authoritative docs.', zh:'啟用領域權威來源 pinning。user_problem 命中 LIBRARIAN_DOMAIN_PINS_PATH 內的 pin 時，列表 URL 會在 search dispatch 前被預先抓取作為 Tier-1 錨點。修補 DDG 只回 Tier-2/3 媒體轉述、漏掉官方 docs 的盲點。'}, type:'boolean' },
  LIBRARIAN_DOMAIN_PINS_PATH: { label:'Domain Pins Path', desc:{en:'Repo-relative or absolute path to the JSON pin file. Format: see crucible/config/domain_pins.json (the repo does not use PyYAML so JSON only).', zh:'JSON pin 設定檔的 repo 相對或絕對路徑。格式參見 crucible/config/domain_pins.json（repo 不裝 PyYAML，所以用 JSON）。'}, type:'text' },
  LIBRARIAN_BILINGUAL_QUERY_EXPANSION: { label:'Bilingual Query', desc:{en:'Auto-issue an English mirror for CJK queries when native-language result count falls below LIBRARIAN_BILINGUAL_QUERY_THRESHOLD. Cross-language results are deduped so the same paper found via Chinese title + English title counts only once. Disable for English-only workloads.', zh:'CJK 查詢的原文結果數低於 LIBRARIAN_BILINGUAL_QUERY_THRESHOLD 時，自動加打英文鏡像。跨語言結果會 dedup — 同論文被中英文標題各命中一次只算一條 citation。純英文工作量可關。'}, type:'boolean' },
  LIBRARIAN_BILINGUAL_QUERY_THRESHOLD: { label:'Bilingual Threshold', desc:{en:'Native-language result count below which the English mirror is issued. Range 1-10; default 3 — only translate when the native search is clearly under-yielding.', zh:'原文結果數低於這個門檻才加打英文鏡像。範圍 1-10，預設 3 — 原文搜索結果明顯不足才翻譯。'}, type:'number' },
  LIBRARIAN_QUERY_TRANSLATE_MODEL: { label:'Translate Model', desc:{en:'LLM model handling CJK to English query translation. Empty = reuse the librarian model. Override with a smaller / cheaper model for translation-only workload.', zh:'處理 CJK 到英文查詢翻譯的 LLM model。空值 = 重用 librarian model。需要用更小 / 更便宜的模型只跑翻譯時改這裡。'}, type:'text' },
  LIBRARIAN_CLAIM_ATTRIBUTION_DIRECTION_KEY: { label:'Per-Direction Attribution', desc:{en:'Per-direction claim attribution. When enabled, the librarian tags each claim with direction key (A..G) and decision field (thesis / primary_metric / fastest_test / major_risk / data_sources) it anchors to. The evidence auditor reads these tags to populate supported_fields — without this, supported_fields is empty for every direction and the force-none gate always triggers (see v1.1.8 diagnostic). Disable only for librarian-prompt regression debugging.', zh:'每個方向的 claim attribution。開啟時 librarian 替每條 claim 標記它支持哪個方向（A..G）的哪個欄位（thesis / primary_metric / fastest_test / major_risk / data_sources）。Evidence auditor 讀這些標記填 supported_fields — 不開的話每個方向 supported_fields 都是空，force-none gate 必然觸發（見 v1.1.8 診斷）。除非 debug librarian prompt 回歸否則不要關。'}, type:'boolean' },

  // v1.1.10 — Librarian Provider Auth.  Both fields are passwords because
  // they are credentials, and both use bilingual desc {en, zh} per the
  // v1.1.0 KEY_META bilingual contract.  Placeholder strings (``replace_*``,
  // ``your_*``, ``xxx*``, ``placeholder*``, ``changeme*``) are filtered out
  // by the backend ``_resolve_*_token`` helpers, so leaving these at their
  // .env.example placeholder value is equivalent to omitting the key.
  CONTEXT7_API_KEY: { label:'Context7 API Key', desc:{en:'Optional Context7 API key from https://context7.com/dashboard. Without a key, context7 search runs on the per-IP anonymous monthly quota; once exhausted every request returns HTTP 429 with a multi-day Retry-After. With a key, requests are charged to the dashboard tier (much higher quota). Leave at the placeholder value to keep anonymous behaviour.', zh:'選填的 Context7 API 金鑰，至 https://context7.com/dashboard 申請（免費）。未填時 context7 搜索使用 per-IP 匿名月度 quota，配額用盡後每次請求都回 HTTP 429 並帶數天的 Retry-After。填入後請求改走 dashboard tier（quota 大幅提高）。保留 placeholder 值等同於不設定，維持匿名行為。'}, type:'password' },
  GITHUB_TOKEN: { label:'GitHub Token (classic PAT)', desc:{en:'Optional GitHub Personal Access Token (classic) used by the librarian search/code + search/repositories endpoints AND by the --github-repo repo analyzer. Generate at GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic). Minimum scope: public_repo. Without it, search/code returns 401 and search/repositories falls back to 10 req/hr; with it, 30 req/min and 5000 req/hr respectively. GH_TOKEN / GITHUB_API_TOKEN accepted as fallback names.', zh:'選填的 GitHub Personal Access Token（classic），librarian 的 search/code + search/repositories 與 --github-repo repo analyzer 都會使用。產生方式：GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)。最低權限：public_repo。未設定時 search/code 回 401、search/repositories 退回匿名（10 req/hr）；設定後分別提升至 30 req/min 與 5000 req/hr。GH_TOKEN / GITHUB_API_TOKEN 為備用名稱。'}, type:'password' },

  // v1.1.8 extended — Direction Gate Tuning (P5).
  CRUCIBLE_DEBATE_TOLERATE_UNVERIFIABLE_EVIDENCE: { label:'Tolerate Unverifiable Evidence', desc:{en:'Master switch for degrade-not-die. 0 (default) = legacy v1.1.7 force-none behaviour: gate kills pipeline if no direction can be defended. 1 = after N consecutive force-none iterations with the same reason, gate picks the highest-scoring direction (even if clamped to 0) and marks it low-confidence instead. Hard feasibility failures are NEVER downgraded.', zh:'degrade-not-die 主開關。0（預設）= v1.1.7 既有 force-none 行為，沒有方向可辯護時 gate 直接中止 pipeline。1 = N 次連續同原因 force-none 後，gate 選 final_score 最高的方向（即使被 clamp 到 0）標 low-confidence 繼續。hard feasibility 失敗永不降級。'}, type:'boolean' },
  CRUCIBLE_DEBATE_DEGRADE_AFTER_N_ITERATIONS: { label:'Degrade After N Iterations', desc:{en:'Number of consecutive force-none iterations with the same gate reason that trigger the degrade path. Range 1-5; default 3 matches DIRECTION_REFINEMENT_MAX_ITERATIONS+1 so degrade only fires when refinement is fully exhausted.', zh:'觸發 degrade path 所需的連續同原因 force-none 次數。範圍 1-5，預設 3 對齊 DIRECTION_REFINEMENT_MAX_ITERATIONS+1，確保 refinement 完全用盡才 degrade。'}, type:'number' },
};

function _toggleSection(hdr) {
  hdr.classList.toggle('open');
  hdr.nextElementSibling.classList.toggle('open');
}

async function loadSettings() {
  try {
    const resp = await fetch('/api/env');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const env = await resp.json();
    State.settingsData = env;
    renderSettings(env);
  } catch (err) {
    console.error('Failed to load settings:', err);
    showToast('Failed to load settings — check server connection', 'error');
    // v1.1.11 (F-A6): replace the "Loading settings…" placeholder so the page
    // does not look hung; the header Reload button re-invokes loadSettings().
    const _c = document.getElementById('settings-container');
    if (_c) {
      _c.innerHTML =
        `<div class="empty-state"><div class="em-icon">❌</div>Failed to load settings — ${escHtml(err.message)}<br>Check the server connection and click Reload.</div>`;
    }
  }
  loadWebhookHistory();
}

// v1.1.0: snapshot of the env values as they appeared when Settings was
// last rendered.  ``saveSettings`` only POSTs keys whose current input
// value differs from this snapshot — protects operator overrides that
// were set via shell ``export`` and would otherwise be silently nuked by
// the empty string in a freshly-rendered (commented-out-in-.env.example)
// input field.
let _SETTINGS_BASELINE = {};

function _snapshotSettingsBaseline() {
  _SETTINGS_BASELINE = {};
  document.querySelectorAll('[data-env-key]').forEach(el => {
    const key = el.dataset.envKey;
    if (!key) return;
    if (el.type === 'checkbox') {
      _SETTINGS_BASELINE[key] = el.checked ? '1' : '0';
    } else {
      _SETTINGS_BASELINE[key] = (el.value == null) ? '' : String(el.value);
    }
  });
}

function renderSettings(env) {
  const container  = document.getElementById('settings-container');
  const activeProvider = (env['LLM_PROVIDER'] || 'openrouter').trim();

  const claimed = new Set(SETTINGS_SCHEMA.flatMap(g => g.keys));

  let html = '';
  SETTINGS_SCHEMA.forEach(grp => {
    const keys = grp.keys.filter(k => k in env || k in KEY_META);
    if (!keys.length) return;

    const isProviderSec = !!grp.provider;
    const isActive      = isProviderSec && grp.provider === activeProvider;
    const open = isProviderSec ? isActive : grp.open;
    const activeClass = isActive ? ' settings-section--active' : (isProviderSec ? ' settings-provider-inactive' : '');
    const activeBadge = isActive ? '<span class="settings-active-badge">ACTIVE</span>' : '';

    const sectionNote = grp.note
      ? `<div class="settings-section-note">ℹ️ ${escHtml(grp.note)}</div>`
      : '';

    html += `<div class="settings-section${activeClass}" data-section-id="${grp.id}">
      <div class="settings-section-hdr${open ? ' open' : ''}" onclick="_toggleSection(this)">
        <span class="settings-section-icon">${grp.icon}</span>
        <span class="settings-section-name">${escHtml(grp.title)}</span>
        ${activeBadge}
        <span class="settings-section-count">${keys.length}</span>
        <span class="settings-section-arrow">▼</span>
      </div>
      <div class="settings-section-body${open ? ' open' : ''}">
        ${sectionNote}
        <div class="settings-grid">`;

    keys.forEach(k => {
      const val  = k in env ? env[k] : '';
      const meta = KEY_META[k] || { label: k, type: 'text' };
      html += _renderKeyItem(k, val, meta);
    });

    html += `</div></div></div>`;
  });

  const unclaimed = Object.keys(env).filter(k => !claimed.has(k));
  if (unclaimed.length) {
    html += `<div class="settings-section" data-section-id="other">
      <div class="settings-section-hdr" onclick="_toggleSection(this)">
        <span class="settings-section-icon">⚙️</span>
        <span class="settings-section-name">Other</span>
        <span class="settings-section-count">${unclaimed.length}</span>
        <span class="settings-section-arrow">▼</span>
      </div>
      <div class="settings-section-body">
        <div class="settings-grid">`;
    unclaimed.forEach(k => {
      html += _renderKeyItem(k, env[k], { label: k, type: 'text' });
    });
    html += `</div></div></div>`;
  }

  container.innerHTML = html;

  const providerEl = document.getElementById('env-LLM_PROVIDER');
  if (providerEl) {
    providerEl.addEventListener('change', () => _updateProviderHighlight(providerEl.value));
  }

  // Capture the rendered state as the baseline for dirty-tracking so
  // saveSettings can detect which inputs the operator actually changed.
  _snapshotSettingsBaseline();
}

function _updateProviderHighlight(activeProvider) {
  SETTINGS_SCHEMA.forEach(grp => {
    if (!grp.provider) return;
    const sec = document.querySelector(`[data-section-id="${grp.id}"]`);
    if (!sec) return;
    const isActive = grp.provider === activeProvider;
    sec.classList.toggle('settings-section--active', isActive);
    sec.classList.toggle('settings-provider-inactive', !isActive);
    const nameEl = sec.querySelector('.settings-section-name');
    let badge = sec.querySelector('.settings-active-badge');
    if (isActive && !badge) {
      badge = document.createElement('span');
      badge.className = 'settings-active-badge';
      badge.textContent = 'ACTIVE';
      nameEl.after(badge);
    } else if (!isActive && badge) {
      badge.remove();
    }
    const hdr  = sec.querySelector('.settings-section-hdr');
    const body = sec.querySelector('.settings-section-body');
    if (isActive) { hdr.classList.add('open');  body.classList.add('open'); }
    else          { hdr.classList.remove('open'); body.classList.remove('open'); }
  });
}

function _renderKeyItem(key, val, meta) {
  const id       = `env-${key}`;
  // v1.1.0 fifth-pass (G-24): broadened secret detection.  The
  // previous regex (/api.?key|secret|token/i) missed common patterns
  // that the v1.1.0 fifth-pass audit found in operator .env files —
  // notably webhook URLs (Slack/Discord/Teams), routing keys
  // (PagerDuty), client_id/dsn (Sentry), private keys, and bearer
  // tokens.  These keys land in the "Other" group when they fall
  // outside SETTINGS_SCHEMA; without the mask flag, real webhook
  // URLs containing authentication path tokens display as plain
  // text on screen — a real shoulder-surf / screen-share leak.
  const isSecret = meta.type === 'password' || /api.?key|secret|token|password|passwd|webhook.?url|routing.?key|bot.?(id|token)|credentials|bearer|signing.?key|private.?key|dsn|auth/i.test(key);
  const badge    = isSecret ? '<span class="settings-secret-badge">secret</span>' : '';
  const descHtml = meta.desc ? `<div class="settings-desc">${escHtml(getDesc(meta.desc))}</div>` : '';
  const hdr      = `<div class="settings-item-hdr">
    <div style="flex:1;min-width:0">
      <div class="settings-item-label">${escHtml(meta.label || key)}</div>
      <div class="settings-item-key">${escHtml(key)}</div>
    </div>
    ${badge}
  </div>${descHtml}`;

  if (meta.type === 'boolean') {
    const checked = (val === '1' || val === 'true') ? 'checked' : '';
    return `<div class="settings-item">
      <div class="settings-bool-row">
        <div style="flex:1;min-width:0">
          <div class="settings-item-label">${escHtml(meta.label || key)}</div>
          <div class="settings-item-key">${escHtml(key)}</div>
        </div>
        <label class="toggle" style="flex-shrink:0">
          <input type="checkbox" id="${id}" data-env-key="${key}" ${checked}>
          <span class="toggle-slider"></span>
        </label>
      </div>
      ${descHtml}
    </div>`;
  }
  if (meta.type === 'select' && meta.opts) {
    const opts = meta.opts.map(o => `<option value="${escHtml(o.v)}"${o.v===val?' selected':''}>${escHtml(o.l)}</option>`).join('');
    return `<div class="settings-item">${hdr}<select id="${id}" data-env-key="${key}">${opts}</select></div>`;
  }
  if (isSecret) {
    // Feature 4: API key test button for recognised providers
    const _API_KEY_PROVIDERS = {
      OPENROUTER_API_KEY:           'openrouter',
      ALIBABA_CODING_PLAN_API_KEY:  'alibaba_coding_plan',
    };
    const testProvider = _API_KEY_PROVIDERS[key] || null;
    const testBtn = testProvider
      ? `<button class="api-test-btn" type="button" data-key-id="${escHtml(id)}" data-provider="${escHtml(testProvider)}"
           onclick="testApiKey(this.dataset.provider, this.dataset.keyId)">Test</button>`
      : '';
    return `<div class="settings-item">
      ${hdr}
      <div style="display:flex;align-items:center;gap:0;">
        <div class="settings-secret-wrap" style="flex:1;">
          <input type="password" id="${id}" data-env-key="${key}" value="${escHtml(val)}" autocomplete="off" placeholder="(not set)">
          <button class="settings-reveal-btn" type="button" title="Show/hide"
            onclick="(function(b){const i=b.previousElementSibling;i.type=i.type==='password'?'text':'password';b.textContent=i.type==='password'?'👁':'🙈';})(this)">👁</button>
        </div>${testBtn}
      </div>
    </div>`;
  }
  if (meta.type === 'number') {
    return `<div class="settings-item">${hdr}<input type="number" id="${id}" data-env-key="${key}" value="${escHtml(val)}" step="any"></div>`;
  }
  return `<div class="settings-item">${hdr}<input type="text" id="${id}" data-env-key="${key}" value="${escHtml(val)}" placeholder="(not set)"></div>`;
}

async function saveSettings() {
  // v1.1.0: only POST keys whose current input value differs from the
  // baseline snapshot captured when the page was rendered.  Previously
  // every ``[data-env-key]`` element was serialised on Save, which meant
  // a freshly-rendered empty input (e.g. for a key that was commented-out
  // in .env.example) would persist ``KEY=""`` into .env even though the
  // operator never touched it — silently nuking any shell-export override.
  // Skipping unchanged keys is a no-op on the happy path and prevents the
  // data-loss class of bugs without changing the legitimate save flow.
  const inputs = document.querySelectorAll('[data-env-key]');
  const data = {};
  let dirtyCount = 0;
  inputs.forEach(el => {
    const key = el.dataset.envKey;
    if (!key) return;
    let current;
    if (el.type === 'checkbox') {
      current = el.checked ? '1' : '0';
    } else {
      current = (el.value == null) ? '' : String(el.value);
    }
    const baseline = (_SETTINGS_BASELINE && (key in _SETTINGS_BASELINE))
      ? _SETTINGS_BASELINE[key]
      : null;
    // null baseline → no snapshot recorded (e.g. snapshot helper hasn't
    // run yet); fall through to legacy "send everything" behaviour for
    // safety.  In the normal flow the baseline is populated immediately
    // after the page renders, so this branch only triggers on a stale
    // DOM that never went through renderSettings().
    if (baseline === null || current !== baseline) {
      data[key] = current;
      dirtyCount += 1;
    }
  });

  if (dirtyCount === 0) {
    showToast('No changes to save', 'info');
    return;
  }

  // Prevent double-submit while request is in-flight
  const saveBtn = document.querySelector('#page-settings .btn-primary');
  if (saveBtn) saveBtn.disabled = true;
  try {
    const resp = await fetch('/api/env', {
      method: 'POST',
      headers: {'Content-Type':'application/json', 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify(data),
    });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const result = await resp.json();
    if (result.success) {
      showToast(`Settings saved to .env (${dirtyCount} key${dirtyCount === 1 ? '' : 's'}) ✓`, 'success');
      // Settings just wrote new values to .env — invalidate the per-run flag
      // panel cache so env-backed flags (Run Insights ledger toggles) on the
      // Idea / Path pages re-sync from the freshly written state.  Fire and
      // forget; the panels stay usable even if this re-render fails.
      try { _refreshEnvCacheAndRerender(); } catch (e) { /* non-fatal */ }
    } else {
      showToast('Save failed: ' + (result.error || 'unknown error'), 'error');
    }
  } catch (err) {
    showToast('Save failed: ' + err.message, 'error');
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

// ─── Toast notification ─────────────────────────────────────────────────────────
function showToast(msg, type = 'success') {
  const palette = {
    success: { bg:'rgba(34,211,160,.2)',  border:'rgba(34,211,160,.4)',  text:'#22d3a0' },
    error:   { bg:'rgba(248,113,113,.2)', border:'rgba(248,113,113,.4)', text:'#f87171' },
    warn:    { bg:'rgba(251,191,36,.15)', border:'rgba(251,191,36,.35)', text:'#fbbf24' },
    info:    { bg:'rgba(96,165,250,.15)', border:'rgba(96,165,250,.35)', text:'#60a5fa' },
  };
  const c = palette[type] || palette.info;
  const region = document.getElementById('toast-region');
  const t = document.createElement('div');
  // When the live region exists, the flex container handles positioning so the
  // toast is static; otherwise fall back to fixed positioning.  (v1.1.11 F-B4)
  t.style.cssText = region
    ? `position:relative; pointer-events:auto; display:flex; align-items:center; gap:10px;
       padding:12px 16px 12px 20px; border-radius:8px; font-size:13px;
       background:${c.bg}; border:1px solid ${c.border}; color:${c.text};
       backdrop-filter:blur(8px); animation: fadeInUp .3s ease;`
    : `position:fixed; bottom:24px; right:24px; z-index:9999;
       padding:12px 20px; border-radius:8px; font-size:13px;
       background:${c.bg}; border:1px solid ${c.border}; color:${c.text};
       backdrop-filter:blur(8px); animation: fadeInUp .3s ease;`;
  const span = document.createElement('span');
  span.textContent = msg;
  t.appendChild(span);
  const x = document.createElement('button');
  x.type = 'button';
  x.textContent = '×';
  x.setAttribute('aria-label', 'Dismiss notification');
  x.style.cssText = `background:none; border:none; color:inherit; cursor:pointer;
    font-size:16px; line-height:1; padding:0 2px; opacity:.7;`;
  x.onclick = () => t.remove();
  t.appendChild(x);
  (region || document.body).appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

// ─── Utility ────────────────────────────────────────────────────────────────────
function escHtml(s) {
  // Escape all five XML/HTML special chars including single quotes so that
  // escHtml() output is safe in both double-quoted AND single-quoted attribute
  // contexts (e.g. onclick="f('${escHtml(x)}')").
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ═══════════════════════════════════════════════════════════════
//  FEATURE 1 – HUMAN-IN-THE-LOOP
// ═══════════════════════════════════════════════════════════════

// Pattern: line contains [AWAIT_INPUT or JSON key __await_input__
function _checkHumanInputSignal(sessId, line) {
  const sess = _getSession(sessId);
  if (!sess) return;

  let prompt = null;
  // Check for JSON object with __await_input__ key
  if (line.includes('__await_input__')) {
    try {
      const obj = JSON.parse(line);
      if (obj && obj.__await_input__) {
        prompt = typeof obj.__await_input__ === 'string' ? obj.__await_input__ : 'Input required:';
      }
    } catch (_) {}
    if (!prompt) prompt = 'Input required:';
  }
  // Check for [AWAIT_INPUT pattern
  if (!prompt) {
    const m = line.match(/\[AWAIT_INPUT[:\s]+([^\]]*)\]/i);
    if (m) prompt = m[1].trim() || 'Input required:';
  }

  if (!prompt) return;

  // Only show banner for the active session on the visible mode
  if (sessId !== State.activeSession[sess.mode]) return;
  const banner = document.getElementById(`hitl-banner-${sess.mode}`);
  const promptEl = document.getElementById(`hitl-prompt-${sess.mode}`);
  const inputEl  = document.getElementById(`hitl-input-${sess.mode}`);
  if (!banner) return;

  // Store the run_id so submit knows which run to signal
  banner.dataset.runId = sess.run_id || '';
  banner.dataset.sessId = sessId;
  if (promptEl) promptEl.textContent = prompt;
  if (inputEl) { inputEl.value = ''; }
  banner.classList.add('hitl-visible');
  if (inputEl) setTimeout(() => inputEl.focus(), 50);
}

async function _submitHitl(mode) {
  const banner  = document.getElementById(`hitl-banner-${mode}`);
  const inputEl = document.getElementById(`hitl-input-${mode}`);
  if (!banner || !inputEl) return;

  // Disable the submit button immediately to prevent double-submission while
  // the fetch is in-flight.  The button is the first <button> child of the banner.
  const submitBtn = banner.querySelector('button');
  if (submitBtn) submitBtn.disabled = true;

  const text  = inputEl.value.trim();
  const runId = banner.dataset.runId;

  if (!runId) {
    if (submitBtn) submitBtn.disabled = false;
    showToast('No active run to signal.', 'warn');
    return;  // keep the banner open and the typed text intact (v1.1.11 F-A5)
  }

  banner.classList.remove('hitl-visible');
  inputEl.value = '';

  try {
    const resp = await fetch(`/api/run/${encodeURIComponent(runId)}/signal`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    showToast('Input submitted to run.', 'success');
  } catch (err) {
    showToast('Failed to submit input: ' + err.message, 'error');
  } finally {
    // Always re-enable the button so the banner is usable for the next signal
    if (submitBtn) submitBtn.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════
//  FEATURE 2 – STAGE TIMING STATS
// ═══════════════════════════════════════════════════════════════

function _renderStageStats(mode, sess) {
  const panel = document.getElementById(`agentflow-panel-${mode}`);
  if (!panel) return;

  // Remove any existing stats panel
  let existing = panel.querySelector('.stage-stats-panel');
  if (existing) existing.remove();

  if (!sess) return;

  const graphDef = AGENT_GRAPHS[sess.analysisType] || AGENT_GRAPHS[1];
  const stageNums = [...new Set(graphDef.nodes.map(n => n.stage))].sort((a, b) => a - b);

  // Build per-stage timing from sess.lines: look for [Stage X or stage=X patterns + timestamps
  // Each line entry is { text, cls }. We compute elapsed by counting lines as a proxy when
  // real timestamps are unavailable, and look for explicit timing tokens (elapsed=Xs, Xms, Xs).
  const stageData = {};

  stageNums.forEach(s => {
    const nodesInStage = graphDef.nodes.filter(n => n.stage === s);
    const stateList = nodesInStage.map(n => (sess.agentStates || {})[n.id] || 'waiting');
    const overallState =
      stateList.includes('error')   ? 'error'   :
      stateList.includes('active')  ? 'active'  :
      stateList.every(st => st === 'done') ? 'done' :
      stateList.every(st => st === 'waiting') ? 'waiting' : 'partial';
    stageData[s] = { stage: s, nodes: nodesInStage, state: overallState, elapsed: null, tokens: null };
  });

  // Scan lines for elapsed time and token hints
  (sess.lines || []).forEach(({ text }) => {
    // elapsed=12.3s or elapsed=1234ms
    const elM = text.match(/elapsed[=:\s]+(\d+(?:\.\d+)?)\s*(ms|s)\b/i);
    if (elM) {
      const val = parseFloat(elM[1]);
      const unit = elM[2].toLowerCase();
      const sec  = unit === 'ms' ? val / 1000 : val;
      // Attribute to the most recently active stage
      const activeStage = Object.values(stageData).find(sd => sd.state === 'active') ||
                          Object.values(stageData).filter(sd => sd.state === 'done').pop();
      if (activeStage && activeStage.elapsed === null) activeStage.elapsed = sec;
    }
    // tokens=1234 or total_tokens=1234
    const tokM = text.match(/(?:total_)?tokens[=:\s]+(\d[\d,]*)/i);
    if (tokM) {
      const n = parseInt(tokM[1].replace(/,/g, ''), 10);
      const activeStage = Object.values(stageData).find(sd => sd.state === 'active') ||
                          Object.values(stageData).filter(sd => sd.state === 'done').pop();
      if (activeStage && activeStage.tokens === null && isFinite(n)) activeStage.tokens = n;
    }
  });

  const visibleStages = Object.values(stageData).filter(sd => sd.state !== 'waiting');
  if (!visibleStages.length) return;

  const stageNames = {
    0: 'Direction Seed', 1: 'Librarian', 2: 'Research Swarm', 3: 'Synthesizer',
    4: 'Dir. Judge', 5: 'Analysis', 6: 'Assembler', 7: 'Gate', 8: 'Code Gen', 9: 'Self-Check',
  };

  const rows = visibleStages.map(sd => {
    const name  = escHtml(stageNames[sd.stage] || `Stage ${sd.stage}`);
    const elapsed = sd.elapsed != null ? sd.elapsed.toFixed(1) + 's' : '—';
    const tokens  = sd.tokens  != null ? sd.tokens.toLocaleString()  : '—';
    const icon = sd.state === 'done' ? '✓' : sd.state === 'active' ? '●' : sd.state === 'error' ? '✗' : '?';
    const rowCls = `stage-stats-${sd.state}`;
    return `<tr class="${rowCls}"><td>${icon}</td><td>${name}</td><td>${elapsed}</td><td>${tokens}</td></tr>`;
  }).join('');

  const statsEl = document.createElement('div');
  statsEl.className = 'stage-stats-panel';
  statsEl.innerHTML = `
    <table class="stage-stats-table">
      <thead><tr><th></th><th>Stage</th><th>Elapsed</th><th>~Tokens</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  panel.appendChild(statsEl);
}

// ═══════════════════════════════════════════════════════════════
//  FEATURE 3 – RUN COMPARISON
// ═══════════════════════════════════════════════════════════════

async function loadCompare() {
  // Populate datalist with known run IDs
  try {
    const resp = await fetch('/api/runs');
    if (!resp.ok) return;
    const data = await resp.json();
    const runs = Array.isArray(data) ? data : (data.runs || data.saved_runs || []);
    const dl = document.getElementById('compare-runs-datalist');
    if (dl) dl.innerHTML = runs.map(r => `<option value="${escHtml(r.id || r)}"></option>`).join('');
  } catch (_) {}
}

async function runCompare() {
  const idA = (document.getElementById('compare-run-a').value || '').trim();
  const idB = (document.getElementById('compare-run-b').value || '').trim();
  const wrap = document.getElementById('compare-result-wrap');

  if (!idA || !idB) {
    showToast('Please enter both Run IDs.', 'warn');
    return;
  }
  if (idA === idB) {
    showToast('Run IDs must be different.', 'warn');
    return;
  }

  // Discard-stale guard (v1.1.11 F-A3): only the most recent invocation may
  // write the wrap — overlapping request pairs otherwise race to last-writer.
  const _token = ++State._compareToken;

  wrap.innerHTML = '<div class="empty-state"><div class="em-icon">⏳</div>Loading both runs…</div>';

  try {
    const [rA, rB] = await Promise.all([
      fetch(`/api/run/${encodeURIComponent(idA)}/detail`).then(r => { if (!r.ok) throw new Error(`Run A: ${r.status}`); return r.json(); }),
      fetch(`/api/run/${encodeURIComponent(idB)}/detail`).then(r => { if (!r.ok) throw new Error(`Run B: ${r.status}`); return r.json(); }),
    ]);
    if (_token !== State._compareToken) return;  // a newer runCompare() superseded us
    if (rA.error) throw new Error('Run A: ' + rA.error);
    if (rB.error) throw new Error('Run B: ' + rB.error);
    wrap.innerHTML = renderComparePage(idA, idB, rA, rB);
  } catch (err) {
    if (_token !== State._compareToken) return;
    wrap.innerHTML = `<div class="empty-state"><div class="em-icon">❌</div>${escHtml(err.message)}</div>`;
  }
}

function renderComparePage(idA, idB, dataA, dataB) {
  const fA = dataA.files || {};
  const fB = dataB.files || {};
  const anA = fA.analysis || {};
  const anB = fB.analysis || {};
  const meA = fA.meta    || {};
  const meB = fB.meta    || {};

  const fields = [
    { label: 'Mode',          va: meA.mode || anA.mode_used, vb: meB.mode || anB.mode_used, type: 'str' },
    { label: 'Provider',      va: meA.llm_provider,           vb: meB.llm_provider,           type: 'str' },
    { label: 'Risk Level',    va: anA.risk_level,              vb: anB.risk_level,              type: 'str' },
    { label: 'Gate Decision', va: anA.gate_decision,           vb: anB.gate_decision,           type: 'str' },
    { label: 'Quality Score', va: anA.score,                   vb: anB.score,                   type: 'num', higherBetter: true },
    // v1.0.5 round 4: prefer USD-explicit cost field with legacy fallback;
    // 6 decimals to match cost_tracker precision (toFixed(5) silently
    // dropped per-call OpenRouter costs at the 6th decimal to $0).
    { label: 'Total Cost',    va: (meA.total_cost_usd != null ? meA.total_cost_usd : meA.total_cost), vb: (meB.total_cost_usd != null ? meB.total_cost_usd : meB.total_cost), type: 'num', higherBetter: false, fmt: v => v != null ? '$' + Number(v).toFixed(6) : '—' },
    { label: 'Total Tokens',  va: meA.total_tokens,            vb: meB.total_tokens,            type: 'num', higherBetter: false, fmt: v => v != null ? Number(v).toLocaleString() : '—' },
    { label: 'Code Files',    va: (dataA.code_files || []).length, vb: (dataB.code_files || []).length, type: 'num', higherBetter: true },
  ];

  function fmtVal(f, v) {
    if (f.fmt) return f.fmt(v);
    if (v == null || v === '') return '—';
    if (f.type === 'num') { const n = Number(v); return isFinite(n) ? n.toFixed(3) : String(v); }
    return String(v);
  }

  function diffClass(f, isA) {
    if (f.type !== 'num') return 'compare-diff-same';
    const na = Number(f.va), nb = Number(f.vb);
    if (!isFinite(na) || !isFinite(nb) || na === nb) return 'compare-diff-same';
    const aBetter = f.higherBetter ? na > nb : na < nb;
    return isA ? (aBetter ? 'compare-diff-better' : 'compare-diff-worse')
               : (aBetter ? 'compare-diff-worse'  : 'compare-diff-better');
  }

  function valClass(f, isA) {
    if (f.type !== 'num') return '';
    const na = Number(f.va), nb = Number(f.vb);
    if (!isFinite(na) || !isFinite(nb) || na === nb) return '';
    const aBetter = f.higherBetter ? na > nb : na < nb;
    return isA ? (aBetter ? 'cmp-val-better' : 'cmp-val-worse')
               : (aBetter ? 'cmp-val-worse'  : 'cmp-val-better');
  }

  const rowsA = fields.map(f => `
    <div class="compare-kv-row ${diffClass(f, true)}">
      <span class="compare-kv-label">${escHtml(f.label)}</span>
      <span class="compare-kv-value ${valClass(f, true)}">${escHtml(fmtVal(f, f.va))}</span>
    </div>`).join('');

  const rowsB = fields.map(f => `
    <div class="compare-kv-row ${diffClass(f, false)}">
      <span class="compare-kv-label">${escHtml(f.label)}</span>
      <span class="compare-kv-value ${valClass(f, false)}">${escHtml(fmtVal(f, f.vb))}</span>
    </div>`).join('');

  return `<div class="compare-diff-grid">
    <div class="compare-col">
      <div class="compare-col-title">🅐 <span style="font-family:'JetBrains Mono',monospace;font-size:11px;">${escHtml(idA)}</span></div>
      <div class="compare-kv">${rowsA}</div>
    </div>
    <div class="compare-col">
      <div class="compare-col-title">🅑 <span style="font-family:'JetBrains Mono',monospace;font-size:11px;">${escHtml(idB)}</span></div>
      <div class="compare-kv">${rowsB}</div>
    </div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════
//  FEATURE 4 – API KEY TEST
// ═══════════════════════════════════════════════════════════════

async function testApiKey(provider, keyId) {
  const inputEl = document.getElementById(keyId);
  if (!inputEl) { showToast('Key input not found.', 'error'); return; }
  const apiKey = inputEl.value.trim();
  if (!apiKey) { showToast('Enter an API key first.', 'warn'); return; }

  // Providers that need a base URL — read its current value from the settings form
  const _PROVIDER_BASE_URL_INPUT = {
    'alibaba_coding_plan': 'env-ALIBABA_CODING_PLAN_BASE_URL',
    'ollama':              'env-OLLAMA_BASE_URL',
  };
  const baseUrlId = _PROVIDER_BASE_URL_INPUT[provider];
  const baseUrl = baseUrlId
    ? ((document.getElementById(baseUrlId) || {}).value || '').trim()
    : '';

  showToast(`Testing ${provider} key…`, 'info');
  const t0 = Date.now();
  try {
    const resp = await fetch('/api/env/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, api_key: apiKey, base_url: baseUrl }),
    });
    const ms = Date.now() - t0;
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    if (data.valid || data.success) {
      showToast(`${provider} key valid ✓  (${ms}ms)`, 'success');
    } else {
      showToast(`${provider} key invalid: ${data.error || 'rejected by provider'} (${ms}ms)`, 'error');
    }
  } catch (err) {
    const ms = Date.now() - t0;
    showToast(`Test failed: ${err.message} (${ms}ms)`, 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
//  FEATURE 7 – A/B TEST
// ═══════════════════════════════════════════════════════════════

// Internal A/B state
const _ABState = { abId: null, pollTimer: null };

function _initABTest() {
  // Wire up mode selectors to show/hide idea vs path fields.
  // Use a named handler stored on the element itself to prevent duplicate
  // listeners accumulating each time the user navigates to this page.
  ['a', 'b'].forEach(variant => {
    const sel = document.getElementById(`ab-mode-${variant}`);
    if (!sel) return;
    // Remove previously-attached handler (if any) before re-adding.
    if (sel._abChangeHandler) sel.removeEventListener('change', sel._abChangeHandler);
    sel._abChangeHandler = () => _abToggleFields(variant, sel.value);
    sel.addEventListener('change', sel._abChangeHandler);
    _abToggleFields(variant, sel.value);
  });
}

function _abToggleFields(variant, mode) {
  const ideaField = document.getElementById(`ab-idea-field-${variant}`);
  const pathField = document.getElementById(`ab-path-field-${variant}`);
  if (ideaField) ideaField.style.display = mode === 'idea'    ? '' : 'none';
  if (pathField) pathField.style.display = mode === 'project' ? '' : 'none';
}

async function startABTest() {
  const buildVariant = variant => {
    const modeEl = document.getElementById(`ab-mode-${variant}`);
    const typeEl = document.getElementById(`ab-type-${variant}`);
    if (!modeEl || !typeEl) throw new Error(`A/B form elements missing for variant ${variant.toUpperCase()}`);
    const mode = modeEl.value;
    const type = parseInt(typeEl.value, 10);
    const idea = (document.getElementById(`ab-idea-${variant}`) || {}).value || '';
    const path = (document.getElementById(`ab-path-${variant}`) || {}).value || '';
    if (mode === 'idea' && !idea.trim()) throw new Error(`Variant ${variant.toUpperCase()}: idea text is empty.`);
    if (mode === 'project' && !path.trim()) throw new Error(`Variant ${variant.toUpperCase()}: project path is empty.`);
    return { mode, analysis_type: type, idea: idea.trim(), project_path: path.trim(), flags: {} };
  };

  let payloadA, payloadB;
  try {
    payloadA = buildVariant('a');
    payloadB = buildVariant('b');
  } catch (err) {
    showToast(err.message, 'warn');
    return;
  }

  const btn = document.getElementById('btn-ab-run');
  btn.disabled = true;
  document.getElementById('ab-status').innerHTML =
    '<span class="status-pill status-running"><span class="pulse"></span>Starting…</span>';
  document.getElementById('ab-results-wrap').style.display = 'none';

  try {
    const resp = await fetch('/api/ab-test/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ variant_a: payloadA, variant_b: payloadB }),
    });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    _ABState.abId = data.ab_id || data.id;
    document.getElementById('ab-results-wrap').style.display = '';
    document.getElementById('ab-winner-wrap').innerHTML = '';
    document.getElementById('ab-metrics-a').innerHTML = '<div class="empty-state" style="padding:16px;">Running…</div>';
    document.getElementById('ab-metrics-b').innerHTML = '<div class="empty-state" style="padding:16px;">Running…</div>';
    showToast('A/B test started.', 'success');
    if (_ABState.pollTimer) clearInterval(_ABState.pollTimer);
    _ABState.pollTimer = setInterval(() => pollABTest(_ABState.abId), 4000);
  } catch (err) {
    btn.disabled = false;
    document.getElementById('ab-status').innerHTML = '';
    showToast('Failed to start A/B test: ' + err.message, 'error');
  }
}

async function pollABTest(abId) {
  if (!abId) return;
  try {
    // Backend route is /api/ab-test/<ab_id> (not /api/abtest/<ab_id>)
    const resp = await fetch(`/api/ab-test/${encodeURIComponent(abId)}`);
    if (!resp.ok) {
      // On persistent error, stop polling so we don't leak the timer
      if (_ABState.pollTimer) { clearInterval(_ABState.pollTimer); _ABState.pollTimer = null; }
      document.getElementById('btn-ab-run').disabled = false;
      return;
    }
    const data = await resp.json();
    if (data.error) {
      if (_ABState.pollTimer) { clearInterval(_ABState.pollTimer); _ABState.pollTimer = null; }
      document.getElementById('btn-ab-run').disabled = false;
      return;
    }

    renderABResults(data);

    // The API returns per-run statuses in data.a.status and data.b.status,
    // not a single top-level data.status.  Both runs must be terminal for us
    // to stop polling.
    const _terminal = s => s === 'done' || s === 'error' || s === 'cancelled';
    const aStatus = data.a && data.a.status;
    const bStatus = data.b && data.b.status;
    const done = _terminal(aStatus) && _terminal(bStatus);
    if (done) {
      if (_ABState.pollTimer) { clearInterval(_ABState.pollTimer); _ABState.pollTimer = null; }
      document.getElementById('btn-ab-run').disabled = false;
      document.getElementById('ab-status').innerHTML =
        `<span class="status-pill status-done"><span class="pulse"></span>Completed</span>`;
    }
  } catch (_) {
    // On unexpected exception, stop polling to avoid leaking the timer
    if (_ABState.pollTimer) { clearInterval(_ABState.pollTimer); _ABState.pollTimer = null; }
    document.getElementById('btn-ab-run').disabled = false;
  }
}

function renderABResults(data) {
  const fmtF2  = v => { const n = Number(v); return (v != null && isFinite(n)) ? n.toFixed(3) : '—'; };
  const fmtPct = v => { const n = Number(v); return (v != null && isFinite(n)) ? (n * 100).toFixed(2) + '%' : '—'; };

  // Backend response shape: { ab_id, run_id_a, run_id_b, a: {status, cost, quality, returncode}, b: {...} }
  function metricsHtml(runData) {
    if (!runData) return '<div class="empty-state" style="padding:16px;">No data yet</div>';
    const items = [
      ['Status',        escHtml(runData.status  || '—')],
      ['Quality Score', fmtF2(runData.quality)],
      ['Total Cost',    runData.cost != null ? '$' + Number(runData.cost).toFixed(6) : '—'],
      ['Return Code',   runData.returncode != null ? String(runData.returncode) : '—'],
    ];
    return items.map(([l, v]) => `
      <div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border);font-size:12px;">
        <span style="color:var(--text-muted);">${escHtml(l)}</span>
        <span style="font-weight:600;font-family:'JetBrains Mono',monospace;font-size:11px;">${v}</span>
      </div>`).join('');
  }

  const rA = data.a;
  const rB = data.b;
  document.getElementById('ab-metrics-a').innerHTML = metricsHtml(rA);
  document.getElementById('ab-metrics-b').innerHTML = metricsHtml(rB);

  // Winner banner — backend never returns data.winner, so derive it client-side
  // from quality scores once both runs have reached a terminal state.
  const winnerWrap = document.getElementById('ab-winner-wrap');
  if (data.winner) {
    const label = data.winner === 'a' ? 'Variant A' : data.winner === 'b' ? 'Variant B' : 'Tie';
    winnerWrap.innerHTML = `<div class="ab-winner-banner">Winner: ${escHtml(label)}${data.winner_reason ? ' — ' + escHtml(data.winner_reason) : ''}</div>`;
  } else {
    const _terminal = s => s === 'done' || s === 'error' || s === 'cancelled';
    if (rA && rB && _terminal(rA.status) && _terminal(rB.status)) {
      const qa = rA.quality != null ? Number(rA.quality) : null;
      const qb = rB.quality != null ? Number(rB.quality) : null;
      let label = '';
      if (qa != null && qb != null && isFinite(qa) && isFinite(qb)) {
        label = qa > qb ? 'Variant A' : qb > qa ? 'Variant B' : 'Tie';
      }
      winnerWrap.innerHTML = label
        ? `<div class="ab-winner-banner">Winner: ${escHtml(label)}</div>`
        : '';
    } else {
      winnerWrap.innerHTML = '';
    }
  }
}

// ═══════════════════════════════════════════════════════════════
//  FEATURE 9 – BUDGET STATUS
// ═══════════════════════════════════════════════════════════════

async function loadBudgetStatus() {
  try {
    const resp = await fetch('/api/budget/status');
    if (!resp.ok) return; // endpoint may not exist yet — silently skip
    const data = await resp.json();
    if (data.error) return;

    const bar = document.getElementById('budget-bar');
    if (!bar) return;
    bar.style.display = '';

    // API returns { today: {cost, run_count}, month: {cost, run_count}, all_time: {cost, run_count} }
    // cost may be null (no runs yet) — guard every Number() call so NaN never reaches toFixed().
    const _costOf = obj => {
      if (obj == null) return 0;
      // Nested object format: { cost: float|null, run_count: int }
      const v = typeof obj === 'object' ? obj.cost : obj;
      const n = Number(v);
      return isFinite(n) ? n : 0;
    };
    const todayCost  = _costOf(data.today_cost  ?? data.today);
    const monthCost  = _costOf(data.month_cost  ?? data.month);
    const dailyCap   = _costOf(data.daily_limit ?? data.budget_daily_limit);

    const todayBadge = document.getElementById('budget-today-badge');
    const monthBadge = document.getElementById('budget-month-badge');
    const capBadge   = document.getElementById('budget-cap-badge');
    const progWrap   = document.getElementById('budget-progress-wrap');
    const progFill   = document.getElementById('budget-progress-fill');
    const capPct     = document.getElementById('budget-cap-pct');

    // Display "—" when there is genuinely no cost data yet (cost === null from server)
    // v1.0.5 round 4: 6 decimals to match cost_tracker precision (toFixed(4)
    // silently dropped per-call OpenRouter cost at the 6th decimal to $0).
    const _fmtCost = (obj) => {
      if (obj == null) return '—';
      const v = typeof obj === 'object' ? obj.cost : obj;
      if (v == null) return '—';
      const n = Number(v);
      return isFinite(n) ? '$' + n.toFixed(6) : '—';
    };
    if (todayBadge) todayBadge.textContent = `Today: ${_fmtCost(data.today_cost ?? data.today)}`;
    if (monthBadge) monthBadge.textContent = `Month: ${_fmtCost(data.month_cost ?? data.month)}`;

    if (dailyCap > 0) {
      const ratio = todayCost / dailyCap;
      const pct   = Math.min(100, ratio * 100);
      const isWarn = ratio >= 0.8 && ratio < 1.0;
      const isOver = ratio >= 1.0;

      if (capBadge) {
        capBadge.style.display = '';
        capBadge.textContent = `Cap: $${dailyCap.toFixed(4)}`;
        capBadge.className = 'budget-badge ' + (isOver ? 'budget-badge-over' : isWarn ? 'budget-badge-warn' : 'budget-badge-normal');
      }
      if (todayBadge) {
        todayBadge.className = 'budget-badge ' + (isOver ? 'budget-badge-over' : isWarn ? 'budget-badge-warn' : 'budget-badge-normal');
      }
      if (progWrap) progWrap.style.display = '';
      if (progFill) {
        progFill.style.width = pct.toFixed(1) + '%';
        progFill.className = 'budget-progress-fill' + (isOver ? ' bp-over' : isWarn ? ' bp-warn' : '');
      }
      if (capPct) capPct.textContent = pct.toFixed(1) + '% of daily cap';
    }
  } catch (_) {
    // Non-critical; /api/budget/status may not be implemented yet
  }
}

// ═══════════════════════════════════════════════════════════════
//  FEATURE 10 – WEBHOOK HISTORY
// ═══════════════════════════════════════════════════════════════

async function loadWebhookHistory() {
  const wrap = document.getElementById('webhook-history-wrap');
  if (!wrap) return;
  wrap.innerHTML = '<div class="empty-state"><div class="em-icon">⏳</div>Loading…</div>';
  try {
    const resp = await fetch('/api/webhook/history');
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    renderWebhookHistoryTable(data.entries || data.history || []);
  } catch (err) {
    wrap.innerHTML = `<div class="empty-state"><div class="em-icon">📭</div>${escHtml(err.message)}</div>`;
  }
}

function renderWebhookHistoryTable(entries) {
  const wrap = document.getElementById('webhook-history-wrap');
  if (!wrap) return;
  if (!entries || entries.length === 0) {
    wrap.innerHTML = '<div class="empty-state"><div class="em-icon">📭</div>No webhook deliveries recorded yet.</div>';
    return;
  }
  const rows = entries.slice(0, 20).map(e => {
    const _tsVal  = e.ts ?? e.timestamp;
    const ts      = _tsVal ? new Date(_tsVal * 1000).toLocaleString() : '—';
    const url     = escHtml(String(e.url || '—').slice(0, 60) + (String(e.url || '').length > 60 ? '…' : ''));
    const status  = e.status_code || e.status || '—';
    const attempt = e.attempt || e.attempt_number || 1;
    const ok      = e.success || (Number(status) >= 200 && Number(status) < 300);
    const badge   = ok
      ? `<span class="wh-status-ok">✓ ${escHtml(String(status))}</span>`
      : `<span class="wh-status-fail">✗ ${escHtml(String(status))}</span>`;
    return `<tr>
      <td>${escHtml(String(ts))}</td>
      <td style="font-family:'JetBrains Mono',monospace;font-size:10px;">${url}</td>
      <td>${badge}</td>
      <td style="text-align:center;">${escHtml(String(attempt))}</td>
    </tr>`;
  }).join('');
  wrap.innerHTML = `
    <table class="webhook-history-table">
      <thead><tr>
        <th>Time</th><th>URL</th><th>Status</th><th>Attempt</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

async function testWebhook() {
  showToast('Sending test webhook…', 'info');
  try {
    const resp = await fetch('/api/notify/test', { method: 'POST' });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    if (data.success || data.ok) {
      showToast('Test webhook sent successfully ✓', 'success');
    } else {
      showToast('Webhook test failed: ' + (data.error || 'unknown'), 'error');
    }
    // Refresh history after test
    setTimeout(() => loadWebhookHistory(), 1000);
  } catch (err) {
    showToast('Webhook test error: ' + err.message, 'error');
  }
}

// ─── Init ───────────────────────────────────────────────────────────────────────
(function init() {
  // Restore language preference *before* the first flag-panel render so the
  // initial pass already shows the correct language (no re-render flicker).
  // initLanguage() does an instant localStorage read, then async-syncs from
  // .env in the background — if .env disagrees it triggers a re-render.
  initLanguage();

  renderFlagGroups('project', 1);
  renderFlagGroups('idea', 1);

  // After the synchronous first render with FLAG_META.isDefault values, fetch
  // the live /api/env state and re-render so env-backed flags (Run Insights
  // ledger toggles) reflect what the recorder will actually do.  Non-blocking;
  // if the fetch fails the panels keep the hardcoded defaults.
  _refreshEnvCacheAndRerender();

  // v1.0.3: ``window.WEBUI_URL`` is set by an inline script in
  // ``index.html`` from the ``webui_url`` Jinja variable — this file is
  // a static asset and is no longer template-rendered, so the value
  // must be threaded through the global rather than embedded inline.
  const url = window.WEBUI_URL || window.location.host;
  const _domainBadge = document.getElementById('domain-badge');
  if (_domainBadge) _domainBadge.textContent = String(url).replace(/^https?:\/\//, '');

  const style = document.createElement('style');
  style.textContent = '@keyframes fadeInUp { from { opacity:0; transform:translateY(10px); } to { opacity:1; transform:translateY(0); } }';
  document.head.appendChild(style);

  // Load webhook status on startup
  loadWebhookStatus();

  // ─── Page lifecycle handlers ────────────────────────────────────────
  // When the tab is closed (or the user navigates the WHOLE window
  // away) explicitly close every open EventSource so the server-side
  // SSE generator unblocks immediately via GeneratorExit instead of
  // sitting in time.sleep() until the 30-min idle timeout fires.  This
  // keeps long-lived Flask workers from holding orphaned generators
  // when users open/close the WebUI repeatedly.
  window.addEventListener('pagehide', () => {
    Object.keys(State._evtSources).forEach(sessId => {
      try { State._evtSources[sessId].close(); } catch (_) {}
    });
  });

  // When the tab becomes visible again after being backgrounded, the
  // browser may have silently killed the EventSource socket (Chrome's
  // Memory Saver, OS sleep, mobile Safari tab discard, aggressive
  // proxies).  EventSource.readyState === 2 (CLOSED) on a still-running
  // session means the user is staring at a "frozen" terminal — the SSE
  // onerror handler ALREADY scheduled a reconnect, but only after a
  // multi-second backoff, and the watchdog only fires after 10 minutes
  // of total silence.  Force an immediate reconnect on visibility
  // change so the user sees fresh output the moment they refocus the
  // tab.  No-op for sessions whose state is already terminal.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    Object.keys(State._evtSources).forEach(sessId => {
      const es = State._evtSources[sessId];
      if (!es || es.readyState !== 2) return;  // 2 === CLOSED
      const sess = _getSession(sessId);
      if (!sess || !sess.run_id) return;
      if (sess.status === 'running' || sess.status === 'starting') {
        _streamSession(sessId);
      }
    });
  });
})();

// ─── Global tooltip (event delegation on [data-tooltip]) ──────────────────────
(function initTooltip() {
  let tipEl = null;
  function getTip() {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.className = 'tooltip-box';
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }
  function position(e) {
    const tip = getTip();
    const x = e.clientX + 14;
    const y = e.clientY - 10;
    const w = tip.offsetWidth  || 300;
    const h = tip.offsetHeight || 80;
    tip.style.left = Math.min(x, window.innerWidth  - w - 16) + 'px';
    tip.style.top  = Math.max(8, Math.min(y, window.innerHeight - h - 8)) + 'px';
  }
  document.addEventListener('mouseover', e => {
    const t = e.target.closest('[data-tooltip]');
    if (t) {
      const tip = getTip();
      tip.textContent = t.dataset.tooltip;
      tip.style.display = 'block';
      position(e);
    } else {
      if (tipEl) tipEl.style.display = 'none';
    }
  });
  document.addEventListener('mousemove', e => {
    if (tipEl && tipEl.style.display === 'block') position(e);
  });
  document.addEventListener('mouseout', e => {
    const related = e.relatedTarget;
    if (!related || !related.closest('[data-tooltip]')) {
      if (tipEl) tipEl.style.display = 'none';
    }
  });
  // a11y (F-B3): keyboard + touch parity — focusing a [data-tooltip] trigger
  // shows the same help text (positioned relative to the element's box).
  document.addEventListener('focusin', e => {
    const t = e.target.closest('[data-tooltip]');
    if (!t) return;
    const tip = getTip();
    tip.textContent = t.dataset.tooltip;
    tip.style.display = 'block';
    const r = t.getBoundingClientRect();
    const w = tip.offsetWidth || 300, h = tip.offsetHeight || 80;
    tip.style.left = Math.min(r.left, window.innerWidth - w - 16) + 'px';
    tip.style.top  = Math.max(8, (r.bottom + 6 > window.innerHeight - h - 8) ? r.top - h - 6 : r.bottom + 6) + 'px';
  });
  document.addEventListener('focusout', e => {
    if (e.target.closest('[data-tooltip]') && tipEl) tipEl.style.display = 'none';
  });
})();