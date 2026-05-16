"""Regression tests for v1.1.4 — asset / instrument classifier enrichment,
test-pollution isolation, and the one-shot orphan-pruning maintenance
helper.

Background:
* Empirical inspection of the real ``.crucible_insights/`` ledger at
  v1.1.3 ship time showed 897 of 952 (94 %) output events were test
  pollution with ``run_id=""`` — tests that wrote to the operator's real
  ledger instead of redirecting to ``tmp_path`` via
  ``CRUCIBLE_RUN_INSIGHTS_DIR`` per CLAUDE.md § 9.5.
* Two of the three real user runs also had broken classification:
  ``cross_exchange_options_arbitrage`` got both ``asset:crypto`` and
  ``asset:uncategorized`` plus an incorrect ``instrument:perpetual``
  (substring match on "perp" inside an unrelated word); a
  ``liquidity_mining_market_making`` project classified to
  ``asset:uncategorized`` because the v1.1.0 dictionary lacked DeFi /
  market-making vocabulary.

v1.1.4 closes both: classifier vocabulary is broadened, the instrument
matcher uses word boundaries + emits ``instrument:options``, conftest
adds an autouse fixture that points every test's ledger at tmp_path,
and a ``maintenance.prune_orphan_events`` helper rewrites streams to
drop ``run_id=""`` events.

Test groups:
- ``TestCryptoVocabExpansion`` — DeFi / market-making / liquidity-mining
  / chain-name inputs classify as ``crypto``.
- ``TestInstrumentDisambiguation`` — options inputs no longer trigger
  ``instrument:perpetual``; word-bounded matching is structural-pinned.
- ``TestConftestLedgerIsolation`` — autouse fixture redirects writes
  away from the real ledger root.
- ``TestPruneOrphanEvents`` — maintenance helper removes ``run_id=""``
  rows, preserves real-id rows, idempotent on second run.
"""

from __future__ import annotations

import inspect
import json
import os
import unittest
from pathlib import Path

from crucible.features.run_insights.maintenance import prune_orphan_events
from crucible.features.run_insights.schema import (
    _instrument_signals,
    classify_asset_category,
    extract_signals,
)


class TestCryptoVocabExpansion(unittest.TestCase):
    """The v1.1.4 crypto pattern must match DeFi / market-making /
    liquidity-mining vocabulary that the v1.1.0 pattern silently dropped
    to ``uncategorized``."""

    def test_liquidity_mining_classifies_as_crypto(self) -> None:
        self.assertEqual(
            classify_asset_category("Build a liquidity mining yield optimiser"),
            "crypto",
        )

    def test_market_making_classifies_as_crypto(self) -> None:
        # The real user run "liquidity_mining_market_making" was the
        # canonical reproducer at v1.1.4 ship time.
        self.assertEqual(
            classify_asset_category("DeFi market-making strategy for spot pairs"),
            "crypto",
        )

    def test_market_maker_with_space_or_hyphen_both_match(self) -> None:
        for variant in (
            "market maker",
            "market-maker",
            "market making strategy",
            "market-making strategy",
        ):
            with self.subTest(variant=variant):
                self.assertEqual(
                    classify_asset_category(f"Build a {variant} for binance"),
                    "crypto",
                )

    def test_yield_farming_classifies_as_crypto(self) -> None:
        for phrasing in (
            "yield farming aggregator",
            "yield-farm scanner",
            "yield farm rotation strategy",
        ):
            with self.subTest(phrasing=phrasing):
                self.assertEqual(classify_asset_category(phrasing), "crypto")

    def test_on_chain_off_chain_classify_as_crypto(self) -> None:
        self.assertEqual(classify_asset_category("on-chain arbitrage"), "crypto")
        self.assertEqual(classify_asset_category("off-chain settlement risk"), "crypto")

    def test_evm_chains_classify_as_crypto(self) -> None:
        for chain in (
            "ethereum",
            "solana",
            "polygon",
            "avalanche",
            "arbitrum",
            "optimism",
        ):
            with self.subTest(chain=chain):
                self.assertEqual(classify_asset_category(f"trade on {chain}"), "crypto")

    def test_dex_amm_defi_classify_as_crypto(self) -> None:
        for token in ("DEX", "AMM", "DeFi", "stablecoin"):
            with self.subTest(token=token):
                self.assertEqual(
                    classify_asset_category(f"{token} arbitrage opportunity"),
                    "crypto",
                )

    def test_unambiguous_protocols_classify_as_crypto(self) -> None:
        # Curve / Compound / Jupiter are intentionally excluded from the
        # crypto pattern because they collide with English / finance
        # vocabulary (yield curve, compound interest, planet Jupiter).
        # Real Curve-Finance / Compound mentions normally include other
        # DeFi tokens (uniswap / aave / DEX / AMM / impermanent loss
        # is itself unambiguous-DeFi-context — but to keep this regression
        # test purely structural we only assert the protocols that have
        # a unique-to-crypto name).
        for protocol in ("Uniswap", "Aave", "GMX", "dYdX", "PancakeSwap", "Lido"):
            with self.subTest(protocol=protocol):
                self.assertEqual(
                    classify_asset_category(f"trade on {protocol}"),
                    "crypto",
                )

    def test_yield_curve_classifies_as_bonds_not_crypto(self) -> None:
        # "Curve" the DeFi protocol shares its name with "yield curve" —
        # the bonds idiom must win.  See _ASSET_PATTERNS comment.
        self.assertEqual(classify_asset_category("US10Y yield curve"), "bonds")
        self.assertEqual(classify_asset_category("treasury yield curve"), "bonds")

    def test_compound_interest_does_not_classify_as_crypto(self) -> None:
        # "Compound" the DeFi protocol shares its name with "compound
        # interest" — must NOT trigger crypto.
        self.assertEqual(
            classify_asset_category("compound annual return on equity"),
            "equity",
        )

    def test_non_crypto_inputs_still_classify_correctly(self) -> None:
        """Regression guard: the expanded crypto pattern must NOT
        cannibalise gold / forex / equity / bonds classifications."""
        self.assertEqual(classify_asset_category("XAUUSD scalping"), "gold")
        self.assertEqual(classify_asset_category("EURUSD swing"), "forex")
        self.assertEqual(classify_asset_category("S&P 500 mean reversion"), "equity")
        self.assertEqual(classify_asset_category("US10Y yield curve"), "bonds")
        self.assertEqual(classify_asset_category("crude oil futures"), "oil")


class TestInstrumentDisambiguation(unittest.TestCase):
    """The v1.1.4 instrument matcher must use word boundaries (so
    ``perp`` no longer matches inside unrelated words) AND emit
    ``instrument:options`` for option-shaped payoffs.

    The cross_exchange_options_arbitrage real-user run incorrectly got
    ``instrument:perpetual`` at v1.1.0 — fixed by word-bounded regex
    + ``options`` priority slot."""

    def test_options_keyword_emits_instrument_options(self) -> None:
        out = _instrument_signals("cross-exchange options arbitrage")
        self.assertIn("instrument:options", out)
        self.assertNotIn("instrument:perpetual", out)

    def test_call_put_keywords_emit_instrument_options(self) -> None:
        self.assertEqual(_instrument_signals("buy call sell put"), ["instrument:options"])

    def test_cjk_options_keywords_emit_instrument_options(self) -> None:
        # CJK punctuation / spacing isn't lowercased the same way, so the
        # implementation must check raw text for the CJK terms.
        for term in ("選擇權", "选择权"):
            with self.subTest(term=term):
                self.assertIn("instrument:options", _instrument_signals(f"建構 {term} 策略"))

    def test_perp_inside_unrelated_word_does_not_match(self) -> None:
        # v1.1.0 footgun: "perp" matched inside "perpendicular",
        # "perpetually", "supper part", etc.
        for trap in ("perpendicular trend lines", "perpetually rebalancing portfolio"):
            with self.subTest(trap=trap):
                self.assertNotIn("instrument:perpetual", _instrument_signals(trap))

    def test_perpetual_word_still_matches(self) -> None:
        for phrasing in ("BTC perpetual basis", "perp funding rate", "perps arbitrage"):
            with self.subTest(phrasing=phrasing):
                self.assertEqual(_instrument_signals(phrasing), ["instrument:perpetual"])

    def test_spot_inside_unrelated_word_does_not_match(self) -> None:
        # "spotify" / "spotlight" / "hotspot" must not trigger.
        for trap in ("spotify integration", "blind spot detection"):
            with self.subTest(trap=trap):
                # "blind spot" actually has a standalone "spot" word
                # which is a legit match — verify by separating the cases.
                got = _instrument_signals(trap)
                if "spot" in trap.split():
                    # standalone token — legitimate match
                    self.assertEqual(got, ["instrument:spot"])
                else:
                    self.assertNotIn("instrument:spot", got)

    def test_at_most_one_instrument_tag_per_event(self) -> None:
        """Priority order: options > perpetual > futures > spot.  When
        multiple keywords coexist (e.g. "options on perpetual futures"),
        only the highest-priority tag must emit."""
        out = _instrument_signals("options on perpetual futures and spot")
        # exactly one instrument:* tag
        instrument_tags = [t for t in out if t.startswith("instrument:")]
        self.assertEqual(len(instrument_tags), 1)
        self.assertEqual(instrument_tags[0], "instrument:options")

    def test_extract_signals_end_to_end_for_options_arbitrage(self) -> None:
        """End-to-end pin matching the real-user reproducer
        ``cross_exchange_options_arbitrage``."""
        sigs = extract_signals(
            mode="Quant",
            user_problem="cross-exchange options arbitrage on binance and bybit",
            run_meta={"llm_provider": "openrouter"},
        )
        self.assertIn("asset:crypto", sigs)
        self.assertIn("instrument:options", sigs)
        self.assertNotIn("instrument:perpetual", sigs)
        # asset:uncategorized must NOT co-exist with asset:crypto.
        self.assertNotIn("asset:uncategorized", sigs)


class TestConftestLedgerIsolation(unittest.TestCase):
    """The autouse fixture in ``tests/conftest.py`` must redirect
    ``CRUCIBLE_RUN_INSIGHTS_DIR`` to a per-test tmp_path so tests cannot
    pollute the operator's real ``.crucible_insights/`` ledger."""

    def test_env_var_points_at_a_tmp_path(self) -> None:
        ledger = os.environ.get("CRUCIBLE_RUN_INSIGHTS_DIR", "")
        self.assertTrue(ledger, "CRUCIBLE_RUN_INSIGHTS_DIR must be set by autouse fixture")
        p = Path(ledger)
        self.assertTrue(p.exists() and p.is_dir(),
                        f"autouse fixture must create the ledger dir: {p}")
        # The path must NOT point at the operator's real ledger
        # (i.e. a directory named exactly ".crucible_insights" under
        # the repo root).
        repo_root = Path(__file__).resolve().parents[1]
        real_ledger = repo_root / ".crucible_insights"
        self.assertNotEqual(p.resolve(), real_ledger.resolve())
        # And the parent must be a pytest tmp_path-rooted directory.
        # pytest tmp_path always lives under a tmp dir whose name starts
        # with "pytest-".  Walk up until we find that token.
        found_pytest_root = False
        for ancestor in p.resolve().parents:
            if "pytest-" in ancestor.name.lower():
                found_pytest_root = True
                break
        self.assertTrue(found_pytest_root,
                        f"ledger dir must live under a pytest tmp_path: {p}")

    def test_conftest_source_has_autouse_fixture(self) -> None:
        """Structural pin: the conftest.py file must define an autouse
        fixture that monkeypatches CRUCIBLE_RUN_INSIGHTS_DIR.  Without
        this fixture, the 897-of-952 pollution observed at v1.1.4 ship
        time would silently return."""
        cf = (Path(__file__).resolve().parent / "conftest.py").read_text(encoding="utf-8")
        self.assertIn("@pytest.fixture(autouse=True)", cf)
        self.assertIn("CRUCIBLE_RUN_INSIGHTS_DIR", cf)
        self.assertIn("monkeypatch.setenv", cf)


class TestPruneOrphanEvents(unittest.TestCase):
    """``maintenance.prune_orphan_events`` must remove events with
    empty / whitespace ``run_id`` while preserving real events,
    idempotent on a second invocation."""

    def _seed_ledger(self, root: Path, events_by_stream: dict) -> None:
        """Write events_by_stream[stream] (list[dict]) as JSONL files."""
        root.mkdir(parents=True, exist_ok=True)
        for stream, events in events_by_stream.items():
            (root / f"{stream}.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in events),
                encoding="utf-8",
            )

    def _read_run_ids(self, root: Path, stream: str) -> list:
        f = root / f"{stream}.jsonl"
        if not f.exists():
            return []
        return [
            json.loads(line)["run_id"]
            for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_orphan_events_removed_real_events_kept(self, tmp_path=None) -> None:
        """tmp_path is a pytest fixture; provide a parametrised default
        for direct unittest invocation."""
        import tempfile
        tmp_path = Path(tempfile.mkdtemp(prefix="v114_prune_"))
        try:
            self._seed_ledger(tmp_path, {
                "output": [
                    {"run_id": "", "project_name": "test"},
                    {"run_id": "9d3dde5a", "project_name": "real_run_1"},
                    {"run_id": "", "project_name": "banner_test"},
                    {"run_id": "a28ba634", "project_name": "real_run_2"},
                ],
                "params": [
                    {"run_id": "", "project_name": "agent_analysis"},
                    {"run_id": "4b7de832", "project_name": "real_run_3"},
                ],
                "debate": [
                    {"run_id": "9d3dde5a", "project_name": "real_run_1"},
                ],
                # error.jsonl intentionally missing — common when no
                # retry-exhausted errors have happened yet.
            })
            summary = prune_orphan_events(tmp_path)
            self.assertEqual(summary.get("output.jsonl"), 2)
            self.assertEqual(summary.get("params.jsonl"), 1)
            self.assertEqual(summary.get("debate.jsonl"), 0)
            self.assertNotIn("error.jsonl", summary)
            # Real run_ids preserved in order.
            self.assertEqual(self._read_run_ids(tmp_path, "output"),
                             ["9d3dde5a", "a28ba634"])
            self.assertEqual(self._read_run_ids(tmp_path, "params"),
                             ["4b7de832"])
            self.assertEqual(self._read_run_ids(tmp_path, "debate"),
                             ["9d3dde5a"])
        finally:
            import shutil
            shutil.rmtree(tmp_path, ignore_errors=True)

    def test_prune_is_idempotent(self) -> None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp(prefix="v114_prune_idem_"))
        try:
            self._seed_ledger(tmp_path, {
                "output": [
                    {"run_id": "", "project_name": "test"},
                    {"run_id": "9d3dde5a", "project_name": "real_run"},
                ],
            })
            first = prune_orphan_events(tmp_path)
            second = prune_orphan_events(tmp_path)
            self.assertEqual(first.get("output.jsonl"), 1)
            self.assertEqual(second.get("output.jsonl"), 0)
            self.assertEqual(self._read_run_ids(tmp_path, "output"), ["9d3dde5a"])
        finally:
            import shutil
            shutil.rmtree(tmp_path, ignore_errors=True)

    def test_dry_run_does_not_modify_files(self) -> None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp(prefix="v114_prune_dry_"))
        try:
            self._seed_ledger(tmp_path, {
                "output": [
                    {"run_id": "", "project_name": "test"},
                    {"run_id": "abc", "project_name": "real"},
                ],
            })
            original = (tmp_path / "output.jsonl").read_text(encoding="utf-8")
            summary = prune_orphan_events(tmp_path, dry_run=True)
            self.assertEqual(summary.get("output.jsonl"), 1)
            # File on disk must be unchanged.
            after = (tmp_path / "output.jsonl").read_text(encoding="utf-8")
            self.assertEqual(original, after)
        finally:
            import shutil
            shutil.rmtree(tmp_path, ignore_errors=True)

    def test_whitespace_only_run_id_treated_as_orphan(self) -> None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp(prefix="v114_prune_ws_"))
        try:
            self._seed_ledger(tmp_path, {
                "output": [
                    {"run_id": "   ", "project_name": "test"},
                    {"run_id": "\t\t", "project_name": "test"},
                    {"run_id": "real123", "project_name": "real"},
                ],
            })
            summary = prune_orphan_events(tmp_path)
            self.assertEqual(summary.get("output.jsonl"), 2)
            self.assertEqual(self._read_run_ids(tmp_path, "output"), ["real123"])
        finally:
            import shutil
            shutil.rmtree(tmp_path, ignore_errors=True)

    def test_missing_root_returns_empty_summary(self) -> None:
        import tempfile
        tmp_path = Path(tempfile.mkdtemp(prefix="v114_prune_missing_"))
        missing = tmp_path / "does_not_exist"
        try:
            summary = prune_orphan_events(missing)
            self.assertEqual(summary, {})
        finally:
            import shutil
            shutil.rmtree(tmp_path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
