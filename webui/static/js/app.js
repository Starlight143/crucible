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

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const pageEl = document.getElementById('page-' + name);
  if (!pageEl) return;  // unknown page guard
  pageEl.classList.add('active');
  const navEl = document.querySelector(`[data-page="${name}"]`);
  if (navEl) navEl.classList.add('active');
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
    fetch('/api/env', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
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
      { key:'librarian_model',       label:'Librarian Model',       ph:'(default from .env)', kind:'text', modes:'b', types:[1,2,3,4], tip:'Override OPENROUTER_LIBRARIAN_MODEL / provider librarian model for this run only. Sent as --librarian-model flag.' },
      { key:'primary_model',         label:'Analysis Model',        ph:'(default from .env)', kind:'text', modes:'b', types:[1,2,3,4], tip:'Override the primary analysis model for this run only. Sent as --primary-model flag.' },
      { key:'direction_judge_model', label:'Direction Judge Model', ph:'(default from .env)', kind:'text', modes:'b', types:[1,2,3,4], tip:'Override the direction judge model for this run only. Sent as --direction-judge-model flag.' },
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
  <label class="input-label-with-tip">${escHtml(sel.label)}<span class="tip-icon" data-tooltip="${escHtml(getDesc(sel.tip))}">?</span></label>
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
        html += cbItem(mode, k, m.label, m.desc, !!m.isDefault);
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
  <label class="input-label-with-tip">${escHtml(inp.label)}<span class="tip-icon" data-tooltip="${escHtml(getDesc(inp.tip))}">?</span></label>
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
    metaEl.textContent = `${sess.status}  ·  ${elapsed}`;
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
  const labels = { starting:'Starting…', running:'Running…', done:'Completed', error:'Error', cancelled:'Stopped' };
  const pill = `<span class="status-pill status-${status}"><span class="pulse"></span>${labels[status] || status}</span>`;
  if (el) el.innerHTML = pill;
  if (inlineEl) inlineEl.innerHTML = pill;
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
    [/codegen_kickoff_start/i,                            null,         'stage8_active'],
    // codegen_kickoff_done dispatches to the ``codegen_phase_done`` state
    // handler (below): it first marks every stage-8 node ``done``, then
    // activates self_check so stage 9 visibly takes over.  Mirrors the
    // analysis_phase_done / research_phase_done pattern for the codegen
    // lane (without this, stage-8 nodes would be stuck showing 'active'
    // for the rest of the run because nothing else closed them).
    [/codegen_kickoff_done/i,                             null,         'codegen_phase_done'],
    [/codegen_kickoff_failed/i,                           'code_gen',   'error'       ],
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
  document.getElementById(`vtab-terminal-${mode}`).classList.toggle('vt-active', view==='terminal');
  document.getElementById(`vtab-agentflow-${mode}`).classList.toggle('vt-active', view==='agentflow');
  const tw = document.getElementById(`terminal-wrap-${mode}`);
  const ap = document.getElementById(`agentflow-panel-${mode}`);
  if (view === 'terminal') {
    if (tw) tw.style.display = '';
    if (ap) ap.classList.remove('af-visible');
  } else {
    if (tw) tw.style.display = 'none';
    if (ap) ap.classList.add('af-visible');
    _refreshAgentFlow(mode);
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

async function startRun(mode) {
  const flags = collectFlags(mode);
  const analysisType = State.pages[mode].analysisType;
  let payload;
  if (mode === 'project') {
    const path = document.getElementById('project-path').value.trim();
    if (!path) { alert('Please enter a project path.'); return; }
    payload = { mode:'project', analysis_type:analysisType, project_path:path, flags };
  } else {
    const idea = document.getElementById('idea-text').value.trim();
    if (!idea) { alert('Please enter an idea or strategy description.'); return; }
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
  runBtn.disabled = true;

  try {
    const resp = await fetch('/api/run', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)
    });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    if (!data.run_id) throw new Error('Server returned no run_id — cannot stream output.');
    sess.run_id = data.run_id;
    runBtn.disabled = false;
    _appendLine(sess.id, `$ ${data.cmd}`, 'dim');
    _appendLine(sess.id, '', 'dim');
    _setSessionStatus(sess.id, 'running');
    _streamSession(sess.id);
  } catch (err) {
    runBtn.disabled = false;
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
    const _cost = Number(data.total_cost); document.getElementById('stat-cost').textContent = (data.total_cost != null && isFinite(_cost)) ? '$' + _cost.toFixed(4) : '—';
    const _qual = Number(data.avg_quality); document.getElementById('stat-quality').textContent = (data.avg_quality != null && isFinite(_qual)) ? _qual.toFixed(2) : '—';
    document.getElementById('stat-session').textContent = (data.session_runs || []).length;

    State._dashboardRuns = data.saved_runs || [];
    renderRunsTable(State._dashboardRuns);
    renderCharts(data);
  } catch (err) {
    console.error('Dashboard error:', err);
  }
  loadBudgetStatus();
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
    return `<tr style="cursor:pointer;" data-run-id="${escHtml(r.id)}" onclick="openRunDetail(this.dataset.runId)">
      <td class="mono">${escHtml(r.id)} ${hasBt}</td>
      <td>${r.cost != null ? ((n => isFinite(n) ? '$'+n.toFixed(5) : '—')(Number(r.cost))) : '—'}</td>
      <td>${r.quality != null ? ((n => isFinite(n) ? n.toFixed(2) : '—')(Number(r.quality))) : '—'}</td>
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
  overlay.classList.add('open');

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
  const backtest  = files.backtest  || {};
  const codeFiles = detail.code_files || [];

  // Build KV grid for meta/analysis
  const kvPairs = [
    { label: 'Mode',         value: meta.mode || analysis.mode_used || '—' },
    { label: 'Provider',     value: meta.llm_provider || '—' },
    { label: 'Timestamp',    value: meta.timestamp || analysis.timestamp || '—' },
    { label: 'Risk Level',   value: analysis.risk_level || '—' },
    { label: 'Gate Decision',value: analysis.gate_decision || '—' },
    { label: 'Score',        value: (() => { const n = Number(analysis.score); return (analysis.score != null && isFinite(n)) ? n.toFixed(3) : '—'; })() },
    { label: 'Total Cost',   value: (() => { const n = Number(meta.total_cost); return (meta.total_cost != null && isFinite(n)) ? '$' + n.toFixed(5) : '—'; })() },
    { label: 'Total Tokens', value: (() => { const n = Number(meta.total_tokens); return (meta.total_tokens != null && isFinite(n)) ? n.toLocaleString() : '—'; })() },
  ];
  // Feature 8: schema_version
  const sv = analysis.schema_version || meta.schema_version;
  if (sv != null) kvPairs.push({ label: 'Schema Version', value: 'v' + sv });

  const kvHtml = kvPairs.map(kv => `
    <div class="detail-kv">
      <div class="detail-kv-label">${escHtml(kv.label)}</div>
      <div class="detail-kv-value">${escHtml(String(kv.value))}</div>
    </div>`).join('');

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

  document.getElementById('modal-body').innerHTML = `
    <div class="detail-section">
      <div class="detail-section-title">Run Info</div>
      <div class="detail-kv-grid">${kvHtml}</div>
    </div>
    ${consensusRaw ? `<div class="detail-section">
      <div class="detail-section-title">Analysis Consensus</div>
      ${consensusHtml}
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

function closeRunDetailModal(event) {
  // Close on overlay click (not on panel itself) or explicit null call
  if (event && event.target !== document.getElementById('run-detail-modal')) return;
  document.getElementById('run-detail-modal').classList.remove('open');
  if (State._detailChart) { State._detailChart.destroy(); State._detailChart = null; }
  if (State._detailDrawdownChart) { State._detailDrawdownChart.destroy(); State._detailDrawdownChart = null; }
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
    keys:['ENHANCED_BACKTEST_RUNNER',
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
  LLM_PROVIDER:          { label:'Provider',                  desc:'Which provider powers all LLM calls in this run.',                                  type:'select', opts:[{v:'openrouter',l:'OpenRouter'},{v:'alibaba_coding_plan',l:'Alibaba Coding Plan'},{v:'ollama',l:'Ollama (Local)'}] },
  OPENROUTER_API_KEY:               { label:'API Key',                      desc:'OpenRouter API key (openrouter.ai/keys).',                                          type:'password' },
  OPENROUTER_BASE_URL:              { label:'Base URL',                     desc:{en:'OpenRouter API endpoint — every OpenRouter request uses this URL. Override if you use a proxy.', zh:'OpenRouter API 端點。所有 OpenRouter 請求使用此 URL。如使用代理可修改。'},             type:'text' },
  OPENROUTER_PRIMARY_MODEL:         { label:'Primary Model',                desc:'Main analysis model for the workflow pipeline.',                                    type:'text' },
  OPENROUTER_DIRECTION_JUDGE_MODEL: { label:'Direction Judge Model',        desc:'Stage 0 direction debate judge model.',                                             type:'text' },
  OPENROUTER_LIBRARIAN_MODEL:       { label:'Librarian / Research Model',   desc:'Research and document retrieval model.',                                            type:'text' },
  OPENROUTER_LLM_TIMEOUT_SECONDS:   { label:'Request Timeout (s)',          desc:'Max seconds per OpenRouter LLM request before timeout.',                           type:'number' },
  ALIBABA_CODING_PLAN_API_KEY:               { label:'API Key',             desc:'Alibaba DashScope API key for Coding Plan.',                                        type:'password' },
  ALIBABA_CODING_PLAN_BASE_URL:              { label:'Base URL',            desc:'OpenAI-compatible endpoint. Change only if using a proxy.',                         type:'text' },
  ALIBABA_CODING_PLAN_PRIMARY_MODEL:         { label:'Primary Model',       desc:'Main analysis model.',                                                              type:'text' },
  ALIBABA_CODING_PLAN_DIRECTION_JUDGE_MODEL: { label:'Direction Judge',     desc:'Stage 0 direction debate judge model.',                                             type:'text' },
  ALIBABA_CODING_PLAN_LIBRARIAN_MODEL:       { label:'Librarian Model',     desc:'Research and document retrieval model.',                                            type:'text' },
  ALIBABA_CODING_PLAN_LLM_TIMEOUT_SECONDS:  { label:'Request Timeout (s)', desc:'Max seconds per Alibaba LLM request before timeout. Default: 180.',                  type:'number' },
  ALIBABA_CODING_PLAN_INITIAL_RESPONSE_TIMEOUT_SECONDS: { label:'Initial Response Timeout (s)', desc:'Max seconds to wait for the first token from Alibaba. Default: 120.', type:'number' },
  OLLAMA_BASE_URL:               { label:'Ollama Base URL',             desc:'Ollama server API base URL. Default: http://localhost:11434/v1.',                  type:'text' },
  OLLAMA_PRIMARY_MODEL:          { label:'Primary Model',               desc:'Main analysis model (must be pulled: ollama pull <model>).',                       type:'text' },
  OLLAMA_DIRECTION_JUDGE_MODEL:  { label:'Direction Judge Model',       desc:'Stage 0 direction debate judge model.',                                             type:'text' },
  OLLAMA_LIBRARIAN_MODEL:        { label:'Librarian / Research Model',  desc:'Research and document retrieval model.',                                            type:'text' },
  STRICT_JSON:         { label:'Strict JSON',              desc:'Force all LLM responses to be valid JSON.',                type:'boolean' },
  COST_TRACE:          { label:'Cost Trace',               desc:'Log per-call token costs to the output stream.',           type:'boolean' },
  LOCAL_CACHE:         { label:'Local Cache',              desc:'Cache LLM responses to disk to save cost on reruns.',      type:'boolean' },
  CRUCIBLE_LOG_LEVEL:  { label:'Log Level',              desc:'Python logging verbosity level.',                          type:'select', opts:[{v:'DEBUG',l:'DEBUG'},{v:'INFO',l:'INFO'},{v:'WARNING',l:'WARNING'},{v:'ERROR',l:'ERROR'}] },
  CRUCIBLE_JSON_LOGS:  { label:'JSON Logs',              desc:'Emit logs in structured JSON format (for log aggregators).', type:'boolean' },
  LIBRARIAN_INTER_QUERY_DELAY_SECONDS: { label:'Librarian Search Delay (s)', desc:'Minimum delay in seconds between consecutive web-search requests (min 0.5). Increase to 8–15 if DuckDuckGo returns 429 errors. Default: 4.', type:'number' },
  LIBRARIAN_MAX_RESULTS_PER_QUERY:     { label:'Max Results per Query',      desc:'Max search results fetched per individual web query. Default: 3.',                type:'number' },
  LIBRARIAN_MAX_CITATIONS:             { label:'Max Citations',              desc:'Upper bound of raw citation candidates collected. Default: 12.',                   type:'number' },
  LIBRARIAN_MAX_QUERIES_PER_LANE:      { label:'Max Queries per Lane',       desc:'Max search queries fired per research lane. Default: 4.',                          type:'number' },
  LIBRARIAN_HTTP_TIMEOUT_SECONDS:      { label:'HTTP Timeout (s)',           desc:'Timeout for each citation-fetch HTTP request. Default: 15.',                       type:'number' },
  LIBRARIAN_HTTP_MAX_BYTES:            { label:'HTTP Max Bytes',             desc:'Max bytes downloaded per citation URL. Default: 1048576 (1 MB).',                  type:'number' },
  LIBRARIAN_MAX_VERIFIED_CITATIONS:    { label:'Max Verified Citations',     desc:'Max citations kept after verification pass. Default: 6.',                          type:'number' },
  CODEX_ENTRYPOINT:          { label:'Runtime Validation Entrypoint', desc:'Optional override for the runtime validation entry point (e.g. api/main.py:app). Leave blank to use auto-detection.', type:'text' },
  CRUCIBLE_ENV_FILE:  { label:'.env File Path',                desc:'Optional override for the .env file location. Leave blank to use the default (.env in project root).', type:'text' },
  GATE_CONTROL_ENABLED:              { label:'Gate Controller',             desc:'Master switch for the Gate Controller subsystem. Set to false to skip Gate evaluation entirely on every run (default: on).', type:'boolean' },
  SELECTIVE_RERUN_ENABLED:           { label:'Selective Rerun',             desc:'Master switch for the Selective Rerun subsystem. Set to false to disable all gate-triggered reruns even when Gate Controller is enabled (default: on).', type:'boolean' },
  DIRECTION_REFINEMENT_ENABLED:      { label:'Direction Refinement',        desc:'Enable evidence-gap refinement inside Direction Debate.',    type:'boolean' },
  DIRECTION_REFINEMENT_MAX_ITERATIONS: { label:'Max Iterations',            desc:'Max refinement rounds before accepting current direction.',  type:'number' },
  GATE_DIRECTION_FEEDBACK_ENABLED:   { label:'Gate Feedback',               desc:'Allow Gate Controller to bounce analysis back for refinement.', type:'boolean' },
  SELECTIVE_RERUN_MAX_ATTEMPTS:      { label:'Selective Rerun Max',         desc:'Upper bound for Gate-triggered selective reruns.',           type:'number' },
  BUDGET_SOFT_COST_LIMIT:            { label:'Soft Cost Limit (USD)',       desc:'Cumulative spend warning threshold in USD. Pipeline logs a WARNING when exceeded but continues. Leave blank to disable.', type:'number' },
  BUDGET_HARD_COST_LIMIT:            { label:'Hard Cost Limit (USD)',       desc:'Cumulative spend hard cutoff in USD. Pipeline stops immediately with BudgetExceededError when exceeded. Leave blank to disable.', type:'number' },
  BUDGET_MAX_TOTAL_TOKENS:           { label:'Max Total Tokens',            desc:'Maximum total tokens (input + output) allowed per run across all LLM calls. Leave blank to disable.', type:'number' },
  CONVERGENCE_MAX_ITERATIONS:        { label:'Max Agent Iterations',        desc:'Hard cap on agent tick() calls per run. 0 = disabled. Default: 50.', type:'number' },
  CONVERGENCE_TIMEOUT_SECONDS:       { label:'Agent Timeout (s)',           desc:'Wall-clock timeout in seconds for the convergence guard. 0 = disabled. Default: 3600 (1 hour).', type:'number' },
  CONVERGENCE_STALE_THRESHOLD:       { label:'Stale Output Threshold',      desc:'Emit StaleLoopWarning when the same agent output signature repeats this many times consecutively. 0 = disabled. Default: 5.', type:'number' },
  AGENT_KICKOFF_RETRY_ATTEMPTS:        { label:'Max Retry Attempts',          desc:'Maximum number of retry attempts for agent crew kickoff failures. Default: 20.', type:'number' },
  AGENT_KICKOFF_RETRY_BACKOFF_SECONDS: { label:'Base Backoff (s)',            desc:'Initial backoff delay in seconds between retries. Doubles each attempt up to max. Default: 2.0.', type:'number' },
  AGENT_KICKOFF_RETRY_MAX_BACKOFF_SECONDS: { label:'Max Backoff (s)',         desc:'Upper bound for exponential backoff delay. Default: 30.0.',  type:'number' },
  AGENT_KICKOFF_RETRY_JITTER_RATIO:  { label:'Jitter Ratio',                desc:'Random jitter added to backoff (0.0–1.0). Prevents thundering-herd. Default: 0.15.', type:'number' },
  API_VERSION_CHECK_ENABLED:         { label:'Enabled',                     desc:'Check for deprecated API calls after code generation.',      type:'boolean' },
  API_VERSION_CHECK_MAX_LIBRARIES:   { label:'Max Libraries',               desc:'Max libraries to check per run.',                            type:'number' },
  API_VERSION_CHECK_TIMEOUT_SECONDS: { label:'Timeout (s)',                 desc:'HTTP timeout for version check requests.',                   type:'number' },
  API_VERSION_CHECK_CACHE_TTL_HOURS: { label:'Cache TTL (h)',               desc:'How long to cache version check results.',                   type:'number' },
  API_VERSION_CHECK_SEVERITY_THRESHOLD: { label:'Severity Threshold',       desc:'Minimum severity to flag.',                                  type:'select', opts:[{v:'low',l:'Low'},{v:'medium',l:'Medium'},{v:'high',l:'High'}] },
  ENHANCED_SECURITY_SCAN:            { label:'Security Scan',               desc:'Run static security analysis (bandit) on generated code.',   type:'boolean' },
  ENHANCED_DEPLOYMENT_ARTIFACTS:     { label:'Deployment Artifacts',        desc:'Generate Dockerfile, docker-compose, CI workflow after run.',type:'boolean' },
  ENHANCED_PROJECT_MEMORY:           { label:'Project Memory',              desc:'Persist direction decisions and failed experiments across runs.', type:'boolean' },
  ENHANCED_PROJECT_MEMORY_MAX_ENTRIES: { label:'Memory Max Entries',        desc:'Max entries retained (oldest evicted when exceeded).',       type:'number' },
  ENHANCED_GENERATE_TESTS:           { label:'Generate Tests',              desc:'Generate pytest suites for each produced Python file.',       type:'boolean' },
  ENHANCED_GENERATE_TESTS_MAX_FILES: { label:'Max Files',                   desc:'Max source files to generate tests for per run.',            type:'number' },
  ENHANCED_API_AUTOPATCH:            { label:'API Autopatch',               desc:'Auto-patch deprecated API calls found by version check.',    type:'boolean' },
  ENHANCED_INDEPENDENT_VALIDATION:   { label:'Independent Validation',      desc:'Run syntax/pytest/smoke in a subprocess after code gen.',    type:'boolean' },
  ENHANCED_INDEPENDENT_VALIDATION_LLM: { label:'Validation LLM Review',    desc:'Add adversarial LLM code review pass during validation.',    type:'boolean' },
  ENHANCED_INDEPENDENT_VALIDATION_TIMEOUT: { label:'Validation Timeout (s)',desc:'Subprocess timeout for pytest and smoke check phases.',      type:'number' },
  ENHANCED_CI_OUTPUT:                { label:'CI Output',                   desc:'Write github_annotations.txt and ci_summary.md after run.',  type:'boolean' },
  ENHANCED_WATCH_DEBOUNCE_SECONDS:   { label:'Watch Debounce (s)',          desc:'File-change debounce delay for watch subcommand.',           type:'number' },
  ENHANCED_WATCH_TIMEOUT:            { label:'Watch Run Timeout (s)',       desc:'Per-triggered-run subprocess timeout for watch subcommand. Kills hung run and resumes watching. Default: 3600.', type:'number' },
  ENHANCED_BATCH_MAX_WORKERS:        { label:'Batch Max Workers',           desc:'Max parallel workers for batch subcommand (1 = sequential).', type:'number' },
  ENHANCED_AUTO_REMEDIATION:         { label:'Auto Remediation',            desc:'LLM-driven closed-loop fix for HIGH+ security findings.',    type:'boolean' },
  ENHANCED_AUTO_REMEDIATION_MAX_ROUNDS: { label:'Max Rounds',               desc:'Max fix iterations before giving up.',                       type:'number' },
  ENHANCED_DEPENDENCY_AUDIT:         { label:'Dependency Audit',            desc:'Run pip-audit on generated requirements.txt for CVEs.',      type:'boolean' },
  ENHANCED_HTML_REPORT:              { label:'HTML Report',                 desc:'Generate a self-contained HTML run report.',                 type:'boolean' },
  ENHANCED_CODE_QUALITY:             { label:'Code Quality',                desc:'Run AST-based complexity/LOC/nesting analysis.',             type:'boolean' },
  ENHANCED_RUN_REGISTRY:             { label:'Run Registry',                desc:'Index completed runs into a SQLite registry.',               type:'boolean' },
  ENHANCED_INTERACTIVE:              { label:'Interactive Mode',            desc:'Pause before each run for research guidance via stdin.',     type:'boolean' },
  ENHANCED_DEDUP_CHECK:              { label:'Dedup Check',                 desc:'Detect semantically similar past runs before starting.',     type:'boolean' },
  DEDUP_SIMILARITY_THRESHOLD:        { label:'Similarity Threshold',        desc:'Cosine similarity threshold [0.0–1.0] to flag as duplicate.', type:'number' },
  DEDUP_LOOKBACK_DAYS:               { label:'Lookback Days',               desc:'Only compare runs within last N days (0 = no limit).',      type:'number' },
  DEDUP_MAX_CORPUS_RUNS:             { label:'Max Corpus Runs',             desc:'Max past runs included in the similarity corpus.',           type:'number' },
  ENHANCED_BATCH_TIMEOUT:            { label:'Batch Timeout (s)',           desc:'Per-project subprocess timeout for the batch subcommand. Default: 3600.',  type:'number' },
  ENHANCED_POST_CHAT:                { label:'Post-Analysis Chat',          desc:'Start interactive Q&A about the analysis after the run completes. Enable with --post-chat.', type:'boolean' },
  POST_CHAT_CONTEXT_CHARS:           { label:'Post-Chat Context (chars)',    desc:'Max chars of analysis output injected as context for post-analysis chat. Default: 12000.', type:'number' },
  ENHANCED_AGENT_METRICS:            { label:'Agent Metrics',               desc:'Compute and display per-agent token, latency, and task metrics after the run. Enable with --agent-metrics.', type:'boolean' },
  ENHANCED_PROMPT_VERSION_LABEL:     { label:'Prompt Version Label',        desc:'Label recorded alongside the run quality score for prompt A/B comparisons. Enable with --prompt-version-label.', type:'text' },
  ENHANCED_LOCKFILE_GEN:             { label:'Lock-file Generation',        desc:'Generate pyproject.toml + pinned requirements.txt for generated code. Enable with --lockfile-gen.', type:'boolean' },
  PROJECT_MEMORY_PROMPT_CHARS:       { label:'Memory Prompt Budget (chars)', desc:'Max characters of project memory injected into the system prompt per run. Default: 16000 (~4 000 tokens).', type:'number' },
  PIPELINE_PROJECT_PROFILE:          { label:'Project Profile Path',        desc:'Path to a JSON project profile file that overrides pipeline defaults. Leave blank to auto-detect.', type:'text' },
  ENHANCED_BACKTEST_RUNNER:          { label:'Backtest Runner (default)',    desc:'Run automated backtest pipeline (data prep, execution, param sweep, LLM fix loop) by default. Enable with --backtest-runner.', type:'boolean' },
  ENHANCED_GITHUB_REPO:              { label:'GitHub Repo URL',             desc:'GitHub repository URL to analyse and inject as research context. Enable with --github-repo.', type:'text' },
  GITHUB_ANALYZER_TIMEOUT:           { label:'GitHub Analyzer Timeout (s)', desc:'HTTP request timeout in seconds for GitHub API calls during repo analysis. Default: 15.', type:'number' },
  GITHUB_ANALYZER_MAX_RETRIES:       { label:'GitHub Analyzer Retries',     desc:'Max retry attempts on transient GitHub API errors (429/5xx). Default: 2.', type:'number' },
  GITHUB_ANALYZER_CACHE_TTL:         { label:'GitHub Analyzer Cache TTL (s)', desc:'Seconds to cache GitHub API responses (0 = disable cache). Default: 3600.', type:'number' },
  ENHANCED_INGEST_DOCS:              { label:'Document Ingestion (default)', desc:'Inject local documents into the pipeline context by default. Enable with --ingest-docs.', type:'boolean' },
  ENHANCED_INGEST_DOCS_DIR:          { label:'Ingest Docs Directory',       desc:'Directory of documents to ingest (PDF/MD/TXT/DOCX). Read by --ingest-docs.', type:'text' },
  DOCUMENT_INGESTION_MAX_CHARS:      { label:'Max Chars per Document',      desc:'Maximum characters read from a single document during ingestion. Default: 8000.', type:'number' },
  DOCUMENT_INGESTION_TOTAL_CHARS:    { label:'Total Ingestion Budget (chars)', desc:'Maximum total characters across all ingested documents per run. Default: 24000.', type:'number' },
  ENHANCED_MULTILANG_CODEGEN:        { label:'Multi-Lang Codegen (default)', desc:'Generate TypeScript/Go translations of Stage 4 Python output by default. Enable with --multilang-codegen.', type:'boolean' },
  ENHANCED_MULTILANG_LANGS:          { label:'Multi-Lang Target Languages', desc:'Comma-separated target languages for multi-language codegen (default: typescript,go).', type:'text' },
  MULTILANG_MAX_FILES:               { label:'Max Files to Translate',      desc:'Maximum number of Python files sent to LLM for multi-language translation per run. Default: 10.', type:'number' },
  MULTILANG_MAX_CHARS:               { label:'Max Chars per File',          desc:'Maximum characters of source code sent per file to the LLM translator. Default: 4000.', type:'number' },
  MULTILANG_ENABLE_RUST:             { label:'Enable Rust Target',          desc:'Allow Rust 2021 edition as a translation target (experimental). Disabled by default.', type:'boolean' },
  NOTIFY_WEBHOOK_URL:                { label:'Custom Webhook URL',          desc:'Generic webhook called on pipeline completion.',             type:'password' },
  NOTIFY_SLACK_WEBHOOK_URL:          { label:'Slack Webhook URL',           desc:'Slack incoming webhook URL.',                               type:'password' },
  NOTIFY_DISCORD_WEBHOOK_URL:        { label:'Discord Webhook URL',         desc:'Discord incoming webhook URL.',                             type:'password' },
  NOTIFY_ON_FAIL_ONLY:               { label:'Notify on Failure Only',      desc:'Skip notifications for successful runs.',                   type:'boolean' },
  ALPHA_VANTAGE_API_KEY:             { label:'Alpha Vantage API Key',       desc:'Required for alpha_vantage data source.',                   type:'password' },
  ALPHA_VANTAGE_BASE_URL:            { label:'Alpha Vantage Base URL',      desc:'Override only if using a proxy.',                           type:'text' },
  FRED_API_KEY:                      { label:'FRED API Key',                desc:'Federal Reserve Economic Data — optional, increases limits.',type:'password' },
  FRED_BASE_URL:                     { label:'FRED Base URL',               desc:'Override only if using a proxy.',                           type:'text' },
  COINGECKO_BASE_URL:                { label:'CoinGecko Base URL',          desc:'Free tier requires no API key.',                            type:'text' },
  EXTERNAL_DATA_TIMEOUT:             { label:'Request Timeout (s)',         desc:'HTTP fetch timeout shared across all connectors.',          type:'number' },
  EXTERNAL_DATA_MAX_RETRIES:         { label:'Max Retries',                 desc:'Retry attempts for failed external data fetches.',          type:'number' },
  AB_TEST_TIMEOUT:                   { label:'Test Timeout (s)',            desc:'Max seconds per pipeline subprocess in an A/B run.',        type:'number' },
  AB_TEST_PARALLEL:                  { label:'Run in Parallel',             desc:'Run both variants simultaneously (uses more resources).',   type:'boolean' },
  BACKTEST_PARAM_SEARCH:             { label:'Param Search Strategy',       desc:'Hyperparameter search method: grid, random, or bayesian (requires optuna).', type:'select', opts:[{v:'grid',l:'Grid'},{v:'random',l:'Random'},{v:'bayesian',l:'Bayesian (Optuna)'}] },
  BACKTEST_BAYESIAN_N_TRIALS:        { label:'Bayesian Trials',             desc:'Number of Optuna TPE trials when using bayesian search.',   type:'number' },
  BACKTEST_SYMBOL:                   { label:'Ticker Symbol',               desc:'Primary ticker/symbol to download (e.g. SPY, BTC-USD). Default: SPY.', type:'text' },
  BACKTEST_DATA_SOURCE:              { label:'Data Source',                 desc:'Force a specific data source. "auto" lets the runner decide. Default: auto.', type:'select', opts:[{v:'auto',l:'Auto'},{v:'yfinance',l:'yfinance'},{v:'binance',l:'Binance'},{v:'project',l:'Project Files'}] },
  BACKTEST_PERIOD:                   { label:'Download Period',             desc:'yfinance download period (e.g. 2y, 5y, max). "auto" derives from the strategy. Default: auto.', type:'text' },
  BACKTEST_INTERVAL:                 { label:'Candle Interval',             desc:'yfinance candle interval (e.g. 1d, 1h, 5m). "auto" derives from the strategy. Default: auto.', type:'text' },
  BACKTEST_DATA_ROWS:                { label:'Synthetic Rows',              desc:'Rows of synthetic OHLCV fallback data when live fetch fails. Default: 500.', type:'number' },
  BACKTEST_INITIAL_CAPITAL:          { label:'Initial Capital',             desc:'Starting capital (USD) for synthetic / paper trading runs. Default: 100000.', type:'number' },
  BACKTEST_MAX_COMBOS:               { label:'Max Combos',                  desc:'Maximum parameter combinations to evaluate during hyperparameter search. Default: 50.', type:'number' },
  BACKTEST_TARGET_METRIC:            { label:'Target Metric',               desc:'Metric to optimise for during param search (e.g. sharpe_ratio, calmar_ratio, total_return). Default: sharpe_ratio.', type:'text' },
  BACKTEST_FIX_MAX_ROUNDS:           { label:'Fix Max Rounds',              desc:'Max LLM auto-fix iterations if the backtest script fails. Default: 3.', type:'number' },
  BACKTEST_TIMEOUT:                  { label:'Backtest Timeout (s)',        desc:'Max seconds per backtest subprocess before it is killed. Default: 120.', type:'number' },
  PORTFOLIO_REBALANCE_PERIOD:        { label:'Rebalance Period',            desc:'Portfolio rebalancing frequency for combined equity curve.', type:'select', opts:[{v:'daily',l:'Daily'},{v:'weekly',l:'Weekly'},{v:'monthly',l:'Monthly'},{v:'quarterly',l:'Quarterly'},{v:'annual',l:'Annual'}] },
  PORTFOLIO_RISK_FREE_RATE:          { label:'Risk-Free Rate',              desc:'Annualised risk-free rate for Sharpe/Sortino calculations (e.g. 0.04 = 4%).', type:'number' },
  // Quant Analytics Suite — feature enable flags
  ENHANCED_QUANT_ANALYTICS:      { label:'Quant Analytics (default)',  desc:'Run Walk-Forward + Significance Testing after a Quant mode backtest by default. Enable with --quant-analytics.', type:'boolean' },
  ENHANCED_WALK_FORWARD:         { label:'Walk-Forward (default)',      desc:'Enable walk-forward validation within --quant-analytics (default: on when analytics enabled).', type:'boolean' },
  ENHANCED_SIGNIFICANCE_TEST:    { label:'Significance Test (default)', desc:'Enable permutation/bootstrap significance test within --quant-analytics (default: on).', type:'boolean' },
  ENHANCED_REGIME_DETECTION:     { label:'Regime Detection (default)',  desc:'Detect market regimes (bull/bear/sideways) from backtest price data by default. Enable with --regime-detection.', type:'boolean' },
  ENHANCED_FACTOR_ANALYSIS:      { label:'Factor Analysis (default)',   desc:'Run CAPM/Fama-French factor exposure regression by default. Enable with --factor-analysis.', type:'boolean' },
  ENHANCED_TRANSACTION_COST:     { label:'Transaction Cost (default)',  desc:'Run transaction cost sensitivity analysis by default. Enable with --transaction-cost.', type:'boolean' },
  ENHANCED_MONTE_CARLO:          { label:'Monte Carlo (default)',       desc:'Run Monte Carlo simulation and stress tests by default. Enable with --monte-carlo.', type:'boolean' },
  ENHANCED_TEARSHEET:            { label:'Tearsheet (default)',         desc:'Generate rich Markdown strategy tearsheet by default. Enable with --tearsheet.', type:'boolean' },
  ENHANCED_SIGNAL_ANALYSIS:      { label:'Signal Analysis (default)',   desc:'Run signal decay analysis to measure edge half-life by default. Enable with --signal-analysis.', type:'boolean' },
  ENHANCED_COINTEGRATION:        { label:'Cointegration (default)',     desc:'Run cointegration + pairs trading analysis on multi-asset data by default. Enable with --cointegration.', type:'boolean' },
  ENHANCED_DYNAMIC_CORRELATION:  { label:'Dynamic Correlation (default)', desc:'Compute rolling correlation matrix and PCA decomposition by default. Enable with --dynamic-correlation.', type:'boolean' },
  // Quant Analytics Suite env vars
  WALK_FORWARD_N_SPLITS:         { label:'WF Splits',               desc:'Number of IS/OOS rolling splits for walk-forward validation. Default: 5.',            type:'number' },
  WALK_FORWARD_OOS_PCT:          { label:'WF OOS %',                desc:'Fraction of each split used as out-of-sample (0.0–1.0). Default: 0.3.',              type:'number' },
  WALK_FORWARD_IS_PCT:           { label:'WF Use % Splits',         desc:'Use percentage-based IS/OOS splits (true) vs fixed-bar-count splits (false). Default: true.', type:'boolean' },
  WALK_FORWARD_MIN_TRAIN_BARS:   { label:'WF Min Train Bars',       desc:'Minimum number of in-sample bars required per fold; folds below this are skipped. Default: 100.', type:'number' },
  SIG_N_PERMUTATIONS:            { label:'Permutations',            desc:'Number of random permutations for the significance p-value estimate. Default: 1000.',  type:'number' },
  SIG_N_BOOTSTRAP:               { label:'Signal Bootstrap N',      desc:'Bootstrap resamples for signal confidence-interval construction. Default: 1000.',      type:'number' },
  SIG_CONFIDENCE_LEVEL:          { label:'Signal CI Level',         desc:'Confidence level for signal bootstrap CIs (e.g. 0.95 = 95% CI). Default: 0.95.',      type:'number' },
  REGIME_METHOD:                 { label:'Regime Method',           desc:'Default regime detection algorithm: volatility, trend, or hmm. Default: volatility.',  type:'select', opts:[{v:'volatility',l:'Volatility Threshold'},{v:'trend',l:'SMA Trend Band'},{v:'hmm',l:'Baum-Welch HMM'}] },
  REGIME_N_REGIMES:              { label:'HMM Regimes',             desc:'Number of hidden states in the Baum-Welch HMM model. Default: 3.',                    type:'number' },
  REGIME_VOL_WINDOW:             { label:'Vol Window (bars)',        desc:'Rolling window for volatility-threshold regime detection. Default: 20.',               type:'number' },
  REGIME_TREND_WINDOW:           { label:'Trend Window (bars)',      desc:'Rolling window for SMA trend-band regime detection. Default: 50.',                    type:'number' },
  REGIME_LOOKBACK_BARS:          { label:'Regime Lookback (bars)',   desc:'Limit regime detection to last N bars (0 = use all available data). Default: 0.',     type:'number' },
  MC_N_SIMULATIONS:              { label:'MC Paths',                desc:'Number of Monte Carlo bootstrap simulation paths. Default: 5000.',                    type:'number' },
  MC_HORIZON_DAYS:               { label:'MC Horizon (days)',       desc:'Number of trading days to simulate forward in Monte Carlo. Default: 252.',             type:'number' },
  MC_METHOD:                     { label:'MC Method',               desc:'Monte Carlo simulation method. Default: bootstrap (block-resample from actual returns).', type:'select', opts:[{v:'bootstrap',l:'Bootstrap (block-resample)'}] },
  MC_SEED:                       { label:'MC Random Seed',          desc:'Random seed for Monte Carlo reproducibility (-1 = random each run). Default: 42.',    type:'number' },
  FACTOR_RISK_FREE_RATE:         { label:'Factor RF Rate',          desc:'Annualised risk-free rate for CAPM alpha computation (e.g. 0.04). Default: 0.04.',    type:'number' },
  FACTOR_LOOKBACK_DAYS:          { label:'Factor Lookback (days)',  desc:'Number of trading days used for factor regression. Default: 252.',                    type:'number' },
  FACTOR_USE_FF_DATA:            { label:'Use Fama-French Data',    desc:'Download Fama-French factor data for 3-factor/5-factor regression (requires internet). Default: false.', type:'boolean' },
  SIGNAL_HORIZONS:               { label:'Signal Horizons',         desc:'Comma-separated forward-return horizons in days (e.g. 1,2,5,10,20). Default: 1,2,3,5,10,20,40.', type:'text' },
  SIGNAL_MIN_OBSERVATIONS:       { label:'Signal Min Obs',          desc:'Minimum number of observations required per horizon for t-stat. Default: 30.',        type:'number' },
  SIGNAL_SIGNIFICANCE_THRESH:    { label:'Signal Sig Threshold',    desc:'p-value threshold for marking a horizon as statistically significant. Default: 0.05.', type:'number' },
  RISK_METHOD:                   { label:'Risk Method',             desc:'Risk attribution computation method. Default: historical (empirical percentile VaR/CVaR).', type:'select', opts:[{v:'historical',l:'Historical (empirical)'},{v:'parametric',l:'Parametric (normal)'},{v:'ewma',l:'EWMA (exp. weighted)'}] },
  RISK_CONFIDENCE_LEVEL:         { label:'VaR Confidence',          desc:'Confidence level for VaR/CVaR calculations (e.g. 0.95 = 95%). Default: 0.95.',        type:'number' },
  RISK_LOOKBACK_WINDOW:          { label:'Risk Lookback (bars)',     desc:'Rolling window in bars for risk calculations. Default: 252 (1 trading year).',         type:'number' },
  ENHANCED_RISK_ATTRIBUTION:     { label:'Risk Attribution (default)', desc:'Enable --risk-attribution by default on every run without passing the flag explicitly.', type:'boolean' },
  TC_COMMISSION_PCT:             { label:'Commission (%)',           desc:'Commission per trade as a decimal fraction (e.g. 0.001 = 0.1% = 10 bps). Default: 0.001.', type:'number' },
  TC_SLIPPAGE_PCT:               { label:'Slippage (%)',             desc:'Slippage per trade as a decimal fraction (e.g. 0.0005 = 0.05% = 5 bps). Default: 0.0005.', type:'number' },
  TC_SPREAD_BPS:                 { label:'Spread (bps)',             desc:'Bid-ask half-spread in basis points applied to each fill. Default: 2.0.',             type:'number' },
  TC_USE_KYLE_IMPACT:            { label:'Kyle Market Impact',       desc:'Enable non-linear market impact modelling via the Kyle-lambda formula. Default: false.', type:'boolean' },
  TC_KYLE_LAMBDA:                { label:'Kyle Lambda',              desc:'Kyle-lambda market impact coefficient (higher = more impact per unit of volume). Default: 0.1.', type:'number' },
  TC_AVG_DAILY_VOLUME:           { label:'Avg Daily Volume',         desc:'Average daily volume for impact scaling (0 = use strategy default / disable). Default: 0.', type:'number' },
  TC_N_SCENARIOS:                { label:'TC Scenarios',             desc:'Monte Carlo scenarios for transaction cost sensitivity analysis. Default: 10.',        type:'number' },
  TEARSHEET_MONTHLY_RETURNS:     { label:'Monthly Returns',          desc:'Include monthly returns heatmap table in the tearsheet output. Default: true.',        type:'boolean' },
  TEARSHEET_DRAWDOWN_PERIODS:    { label:'Drawdown Periods',         desc:'Include top drawdown periods table in the tearsheet output. Default: true.',           type:'boolean' },
  TEARSHEET_MAX_DRAWDOWN_PERIODS:{ label:'Max Drawdown Rows',        desc:'Number of worst drawdown periods to list in the table. Default: 5.',                  type:'number' },
  TEARSHEET_TRADE_ANALYSIS:      { label:'Trade Analysis',           desc:'Include per-trade statistics (win rate, avg win/loss, profit factor) in the tearsheet. Default: true.', type:'boolean' },
  MLFLOW_TRACKING_URI:               { label:'Tracking URI',                desc:'MLflow server URI. When set, every run is logged as an MLflow experiment.', type:'text' },
  MLFLOW_EXPERIMENT_NAME:            { label:'Experiment Name',             desc:'MLflow experiment name (default: Crucible).',          type:'text' },
  MLFLOW_LOG_ARTIFACTS:              { label:'Log Artifacts',               desc:'Upload the HTML report as an MLflow artifact on completion.', type:'boolean' },
  WEBHOOK_SECRET:                    { label:'HMAC Secret',                 desc:'HMAC-SHA256 secret for POST /webhook/trigger signature validation. Leave blank to disable signature checks.', type:'password' },
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
  }
  loadWebhookHistory();
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
  const isSecret = meta.type === 'password' || /api.?key|secret|token/i.test(key);
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
  const inputs = document.querySelectorAll('[data-env-key]');
  const data = {};
  inputs.forEach(el => {
    const key = el.dataset.envKey;
    if (el.type === 'checkbox') {
      data[key] = el.checked ? '1' : '0';
    } else {
      data[key] = el.value;
    }
  });
  // Prevent double-submit while request is in-flight
  const saveBtn = document.querySelector('#page-settings .btn-primary');
  if (saveBtn) saveBtn.disabled = true;
  try {
    const resp = await fetch('/api/env', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
    if (!resp.ok) throw new Error(`Server error ${resp.status}`);
    const result = await resp.json();
    if (result.success) {
      showToast('Settings saved to .env ✓', 'success');
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
  const t = document.createElement('div');
  t.style.cssText = `
    position:fixed; bottom:24px; right:24px; z-index:9999;
    padding:12px 20px; border-radius:8px; font-size:13px;
    background:${c.bg}; border:1px solid ${c.border}; color:${c.text};
    backdrop-filter:blur(8px); animation: fadeInUp .3s ease;
  `;
  t.textContent = msg;
  document.body.appendChild(t);
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

  banner.classList.remove('hitl-visible');
  inputEl.value = '';

  if (!runId) {
    if (submitBtn) submitBtn.disabled = false;
    showToast('No active run to signal.', 'warn');
    return;
  }

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

  wrap.innerHTML = '<div class="empty-state"><div class="em-icon">⏳</div>Loading both runs…</div>';

  try {
    const [rA, rB] = await Promise.all([
      fetch(`/api/run/${encodeURIComponent(idA)}/detail`).then(r => { if (!r.ok) throw new Error(`Run A: ${r.status}`); return r.json(); }),
      fetch(`/api/run/${encodeURIComponent(idB)}/detail`).then(r => { if (!r.ok) throw new Error(`Run B: ${r.status}`); return r.json(); }),
    ]);
    if (rA.error) throw new Error('Run A: ' + rA.error);
    if (rB.error) throw new Error('Run B: ' + rB.error);
    wrap.innerHTML = renderComparePage(idA, idB, rA, rB);
  } catch (err) {
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
    { label: 'Total Cost',    va: meA.total_cost,              vb: meB.total_cost,              type: 'num', higherBetter: false, fmt: v => v != null ? '$' + Number(v).toFixed(5) : '—' },
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
      ['Total Cost',    runData.cost != null ? '$' + Number(runData.cost).toFixed(5) : '—'],
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
    const _fmtCost = (obj) => {
      if (obj == null) return '—';
      const v = typeof obj === 'object' ? obj.cost : obj;
      if (v == null) return '—';
      const n = Number(v);
      return isFinite(n) ? '$' + n.toFixed(4) : '—';
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

  // v1.0.3: ``window.WEBUI_URL`` is set by an inline script in
  // ``index.html`` from the ``webui_url`` Jinja variable — this file is
  // a static asset and is no longer template-rendered, so the value
  // must be threaded through the global rather than embedded inline.
  const url = window.WEBUI_URL || window.location.host;
  document.getElementById('domain-badge').textContent = url.replace(/^https?:\/\//, '');

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
})();