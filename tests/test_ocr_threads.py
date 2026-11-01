"""The OCR engine is capped at a few onnxruntime threads: one scanned source must not take every core of a host
that runs many workers."""

from tauceti_roadmap_worker import sources


class _Engine:
    def __init__(self, **kw):
        self.kw = kw


class _OldEngine:
    def __init__(self):
        self.kw = {}


def test_engine_gets_the_thread_cap(monkeypatch):
    monkeypatch.delenv("TAUCETI_OCR_THREADS", raising=False)
    e = sources.ocr_engine(_Engine)
    assert e.kw == {"intra_op_num_threads": sources.OCR_THREADS, "inter_op_num_threads": 1}
    monkeypatch.setenv("TAUCETI_OCR_THREADS", "2")
    assert sources.ocr_engine(_Engine).kw["intra_op_num_threads"] == 2


def test_engine_without_keywords_still_loads():
    assert isinstance(sources.ocr_engine(_OldEngine), _OldEngine)
