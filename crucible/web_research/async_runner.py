"""Parallel multi-provider search dispatcher.

v1.1.8 extended (Phase 5, Q5).  Provides a synchronous API that internally
fans out provider calls in parallel using ``ThreadPoolExecutor``.

Why threads instead of ``asyncio``:

* The librarian dispatcher (``section_04``) is sync, called from crewAI
  tool wrappers that are sync.  Wrapping the entire call chain in
  ``async`` would require touching every provider, every helper, every
  caller — far beyond Q5's scope.
* CLAUDE.md § 5 confirms ``ThreadPoolExecutor`` is the established
  pattern in this repo (used by ``features/backtest_runner.py``).
* HTTP requests release the GIL during socket I/O, so threads deliver
  real parallelism for the workload (HTTP-bound) the librarian
  actually does.
* Per-provider concurrency cap is naturally a thread pool size.

The module is OPT-IN via ``LIBRARIAN_ASYNC_FANOUT_ENABLED`` env (default 1).
When disabled, ``multi_provider_search`` falls back to sequential
dispatch — bit-for-bit identical to the legacy v1.1.7 behaviour, useful
for emergency rollback.

Failure modes:

* Per-provider exception in the worker thread → empty list for that
  provider in the result map (other providers unaffected).
* Overall timeout exceeded → providers that haven't finished are
  cancelled and reported as empty.
* No providers given → empty result map.
"""

from __future__ import annotations

import concurrent.futures as _cf
from typing import Any, Callable, Dict, List, Tuple

# Tri-modal import.
try:
    from .._env import env_bool, env_int
    from ..runtime_logging import get_logger
except ImportError:  # pragma: no cover - flat-launcher fallback
    from _env import env_bool, env_int  # type: ignore[no-redef]
    from runtime_logging import get_logger  # type: ignore[no-redef]


LOGGER = get_logger(__name__)


# Signature of one provider search function.  Returns a list of
# ResearchCitation but we avoid the type-import to keep this module
# loose-coupled (it's a generic fan-out — could fan out other functions
# in the future).
SearchFn = Callable[[str], List[Any]]


def async_fanout_enabled() -> bool:
    """Master toggle for parallel fan-out."""
    return env_bool("LIBRARIAN_ASYNC_FANOUT_ENABLED", True)


def _max_workers(providers: int) -> int:
    """Resolve max parallel workers.

    Caps at the number of providers (no point starting more threads
    than work items).  Operator override via
    ``LIBRARIAN_ASYNC_MAX_WORKERS`` (defaults to 8 — large enough for
    every realistic provider mix without runaway thread spawning).
    """
    override = env_int("LIBRARIAN_ASYNC_MAX_WORKERS", 8)
    cap = override if override and override > 0 else 8
    return max(1, min(int(cap), int(providers)))


def multi_provider_search(
    query: str,
    providers_with_funcs: List[Tuple[str, SearchFn]],
    *,
    timeout_seconds: float = 30.0,
) -> Dict[str, List[Any]]:
    """Dispatch *query* across multiple providers in parallel.

    Parameters
    ----------
    query
        The search query string.  Passed unchanged to each provider
        function.
    providers_with_funcs
        List of ``(provider_name, search_fn)`` tuples.  Provider names
        are arbitrary strings; ``search_fn(query)`` must accept the
        query as its only positional arg and return a list of citations
        (or any list — the runner is generic).
    timeout_seconds
        Overall budget across ALL providers.  Once exceeded, providers
        that haven't completed are reported as empty.  Per-provider
        timeouts must be enforced by the provider function itself
        (typically via ``safe_http_*`` timeout).

    Returns
    -------
    Dict[str, List[Any]]
        Map ``provider_name → results``.  Every input provider appears
        in the output, even if it failed (value will be ``[]``).

    Behaviour by mode:

    * ``LIBRARIAN_ASYNC_FANOUT_ENABLED=1`` (default): true parallel
      dispatch via ``ThreadPoolExecutor``.
    * ``LIBRARIAN_ASYNC_FANOUT_ENABLED=0``: sequential dispatch, in
      input order.  Identical timing to the legacy v1.1.7 loop.

    Failure semantics:

    * Per-provider exception → that provider's value is ``[]``.
    * Timeout → providers that haven't finished are ``[]``.  No
      raises.
    """
    if not query or not providers_with_funcs:
        return {}

    if not async_fanout_enabled():
        # Sequential fallback — drop-in replacement for the legacy
        # provider loop's per-query behaviour.  No threads spawned.
        out: Dict[str, List[Any]] = {}
        for name, fn in providers_with_funcs:
            try:
                out[name] = list(fn(query) or [])
            except Exception as exc:
                LOGGER.debug(
                    "async_runner sequential: %s raised: %s", name, exc,
                )
                out[name] = []
        return out

    return _parallel_dispatch(
        query, providers_with_funcs, timeout_seconds=timeout_seconds,
    )


def _parallel_dispatch(
    query: str,
    providers_with_funcs: List[Tuple[str, SearchFn]],
    *,
    timeout_seconds: float,
) -> Dict[str, List[Any]]:
    """Real parallel dispatch via ThreadPoolExecutor.

    Note on timeout behaviour: ``ThreadPoolExecutor.__exit__`` defaults
    to ``wait=True``, which blocks until every running thread finishes —
    making the ``with`` block honour timeout only for the iterator, not
    for shutdown.  We use ``shutdown(wait=False, cancel_futures=True)``
    so the function returns within the budget even if a worker thread
    is mid-``time.sleep()`` or stuck in a slow HTTP call.

    The orphaned threads will eventually complete in the background;
    their work is discarded.  Trade-off accepted because:

    * Workers run our own ``_safe_call`` wrapper, so they don't leak
      exceptions to the main pipeline.
    * HTTP calls have their own per-request timeouts (LIBRARIAN_HTTP_
      TIMEOUT_SECONDS), so the orphaned thread is bounded.
    * The main pipeline's responsiveness matters more than perfectly
      reaping all worker threads.
    """
    results: Dict[str, List[Any]] = {
        name: [] for name, _ in providers_with_funcs
    }
    workers = _max_workers(len(providers_with_funcs))
    ex = _cf.ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="librarian-fanout",
    )
    try:
        futures = {
            ex.submit(_safe_call, name, fn, query): name
            for name, fn in providers_with_funcs
        }
        try:
            for fut in _cf.as_completed(
                futures, timeout=timeout_seconds,
            ):
                name = futures[fut]
                try:
                    results[name] = list(fut.result() or [])
                except Exception as exc:
                    LOGGER.debug(
                        "async_runner parallel: %s raised: %s",
                        name, exc,
                    )
                    results[name] = []
        except _cf.TimeoutError:
            # Overall budget exceeded — cancel anything still pending.
            for fut, name in futures.items():
                if not fut.done():
                    fut.cancel()
                    LOGGER.debug(
                        "async_runner parallel: %s exceeded "
                        "%.1fs budget; cancelled",
                        name, timeout_seconds,
                    )
                    # Leave results[name] = [].
    finally:
        # Don't wait on lingering threads — orphan them.  See docstring
        # for trade-off.  ``cancel_futures=True`` cancels QUEUED but
        # not-yet-started futures; already-running threads continue
        # invisibly until their per-HTTP timeout expires.
        ex.shutdown(wait=False, cancel_futures=True)
    return results


def _safe_call(name: str, fn: SearchFn, query: str) -> List[Any]:
    """Worker wrapper that swallows exceptions so the future returns
    cleanly.

    We could let the exception propagate and catch it in
    ``fut.result()``, but isolating it here keeps the future cleanup
    simple and means a misbehaving provider can't spam an exception
    repr into the future's traceback.
    """
    try:
        result = fn(query)
        return list(result or [])
    except Exception as exc:
        LOGGER.debug("provider %s raised in worker: %s", name, exc)
        return []
