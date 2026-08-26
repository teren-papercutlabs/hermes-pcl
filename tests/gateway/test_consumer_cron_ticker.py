import threading

from gateway import durable_jsonl_consumer as consumer


def test_cron_ticker_continues_after_tick_exception(monkeypatch):
    stop = threading.Event()
    calls = []

    def fake_tick(*, verbose):
        calls.append(verbose)
        if len(calls) == 1:
            raise RuntimeError("transient scheduler fault")
        stop.set()

    monkeypatch.setattr("cron.scheduler.tick", fake_tick)
    consumer._cron_ticker(stop, interval_seconds=0.001)

    assert calls == [False, False]


def test_consumer_cron_ticker_start_and_stop(monkeypatch):
    entered = threading.Event()

    def fake_worker(stop_event, *, interval_seconds):
        entered.set()
        stop_event.wait()

    monkeypatch.setattr(consumer, "_cron_ticker", fake_worker)
    stop, thread = consumer._start_cron_ticker(interval_seconds=0.001)
    assert entered.wait(timeout=1)
    assert thread.is_alive()

    consumer._stop_cron_ticker(stop, thread)
    assert not thread.is_alive()
