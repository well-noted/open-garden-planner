"""The phone's offline outbox (the outbox section of ``static/api.js``) under Node, plus the idempotency it relies on.

Skipped where Node is absent. The queue takes injected storage, sender and timers, so the
ordering, backoff and no-duplicate rules run without a browser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from open_garden_planner.field_remote import static

NODE = shutil.which("node")
OUTBOX_JS = Path(static.__file__).with_name("static") / "api.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is not installed")


def run_js(script: str) -> object:
    code = (
        f"import {{ Outbox, memoryStore, isRetryable }} from {json.dumps(OUTBOX_JS.as_uri())};\n"
        f"(async () => {{ {script} }})();"
    )
    out = subprocess.run(
        [NODE or "node", "--input-type=module", "-e", code], capture_output=True, text=True, timeout=30, check=True
    )
    return json.loads(out.stdout)


PRELUDE = """
const timers = []; const sent = [];
const mk = (respond) => new Outbox({ store: memoryStore(), send: async (e) => respond(e),
  setTimer: (fn, ms) => { timers.push(ms); return timers.length; }, clearTimer: () => {}, random: () => 0.5 });
"""


def test_offline_saves_are_kept_then_sent_in_order() -> None:
    got = run_js(
        PRELUDE
        + """
        let up = false;
        const o = mk((e) => { if (!up) throw new Error('offline'); sent.push(e.body.k); return { ok: true, status: 201, json: {} }; });
        for (const k of ['a', 'b', 'c']) await o.enqueue({ body: { k }, planId: 'p' });
        await o.flush();
        const mid = [o.pending, sent.length];
        up = true;
        const n = await o.flush({ manual: true });
        console.log(JSON.stringify({ mid, n, sent, pending: o.pending }));
        """
    )
    assert got == {"mid": [3, 0], "n": 3, "sent": ["a", "b", "c"], "pending": 0}  # type: ignore[comparison-overlap]


def test_a_transient_failure_stops_the_run_and_backs_off() -> None:
    got = run_js(
        PRELUDE
        + """
        const o = mk((e) => { sent.push(e.body.k); return e.body.k === 'a' ? { ok: false, status: 503, json: null } : { ok: true, status: 201 }; });
        await o.enqueue({ body: { k: 'a' } }); await o.enqueue({ body: { k: 'b' } });
        timers.length = 0;
        await o.flush(); await o.flush();
        console.log(JSON.stringify({ sent, pending: o.pending, timers }));
        """
    )
    assert got["sent"] == ["a", "a"] and got["pending"] == 2  # type: ignore[index]
    assert got["timers"][1] > got["timers"][0] and max(got["timers"]) <= 60000 * 1.25  # type: ignore[index]


def test_a_refused_record_is_set_aside_and_does_not_block_the_rest() -> None:
    got = run_js(
        PRELUDE
        + """
        const o = mk((e) => e.body.k === 'a' ? { ok: false, status: 409, json: { error: 'plan_changed', detail: 'The plan changed.' } } : { ok: true, status: 201 });
        await o.enqueue({ body: { k: 'a' } }); await o.enqueue({ body: { k: 'b' } });
        await o.flush();
        console.log(JSON.stringify({ pending: o.pending, rejected: o.rejected.map((r) => r.rejected) }));
        await o.discardRejected();
        """
    )
    assert got == {"pending": 0, "rejected": ["The plan changed."]}  # type: ignore[comparison-overlap]


def test_retry_classification() -> None:
    got = run_js("console.log(JSON.stringify([0, 401, 429, 500, 503, 409, 422, 413].map(isRetryable)));")
    assert got == [True, True, True, True, True, False, False, False]


def test_resending_the_same_key_never_duplicates(tmp_path: Path) -> None:
    from open_garden_planner.services import field_records as fr

    plan = tmp_path / "plan.ogp"
    plan.write_text("{}", encoding="utf-8")
    data = {
        "kind": "note", "idempotency_key": "abcdefgh12345678", "notes": "x", "status": "pending",
        "source": "field_remote", "geometry": {"type": "point", "lat": 1.0, "lon": 2.0},
    }
    first, created1 = fr.create_record(plan, dict(data), None)
    second, created2 = fr.create_record(plan, dict(data), None)
    assert created1 is True and created2 is False and first.id == second.id


def test_the_api_script_is_served() -> None:
    assert "/static/api.js" in static.ASSETS
