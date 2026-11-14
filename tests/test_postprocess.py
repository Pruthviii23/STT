import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from murmur import postprocess as pp  # noqa: E402


def test_fillers_and_caps():
    assert pp.rule_clean("um i think uh we should go") == "I think we should go."
    assert pp.rule_clean("so, um, buy milk") == "So, buy milk."


def test_repeats():
    assert pp.rule_clean("the the cat sat") == "The cat sat."
    assert pp.rule_clean("he said that that was fine") == "He said that that was fine."


def test_spacing_and_punct():
    assert pp.rule_clean("hello , world .  how are you ?") == "Hello, world. How are you?"
    assert pp.rule_clean("it costs 3.50 today") == "It costs 3.50 today."


def test_commands():
    assert pp.rule_clean("first thing new line second thing") == "First thing\nSecond thing."
    r = pp.rule_clean("shopping list bullet point milk next bullet eggs")
    assert r == "Shopping list\n- Milk\n- Eggs.", repr(r)


def test_empty_and_i():
    assert pp.rule_clean("") == ""
    assert pp.rule_clean("   ") == ""
    assert "I'm" in pp.rule_clean("i'm here. and i'll stay")


def test_meaning_kept():
    s = "Call John at five."
    assert pp.rule_clean(s) == s


def test_llm_never_raises():
    assert pp.llm_clean("some text here ok", "http://127.0.0.1:1", "x", 0.5) is None
    assert pp.llm_clean("some text here ok", "not a url", "x", 0.5) is None


def test_llm_guards():
    import requests
    orig = requests.post

    class R:
        def __init__(self, t): self.t = t
        def raise_for_status(self): pass
        def json(self): return {"response": self.t}

    text = "remind me to call the dentist tomorrow"
    try:
        for resp, ok in [("Remind me to call the dentist tomorrow.", True),
                         ("", False), ("Sure! Remind me to call the dentist tomorrow.", False),
                         ("Here is the corrected text: remind me", False),
                         ("x" * 200, False), ("Remind.", False)]:
            requests.post = lambda *a, _r=resp, **k: R(_r)
            got = pp.llm_clean(text, "http://x", "m", 1)
            assert (got is not None) == ok, (resp, got)
    finally:
        requests.post = orig


def test_clean_threshold():
    cfg = types.SimpleNamespace(llm_cleanup=True, ollama_url="http://127.0.0.1:1",
                                ollama_model="m", llm_timeout_s=0.5)
    assert pp.clean("um hello there", cfg) == "Hello there."
    assert pp.clean("one two three four five six seven", cfg) == "One two three four five six seven."


def test_live_llm():
    import requests
    try:
        requests.get("http://127.0.0.1:11434/api/tags", timeout=2).raise_for_status()
    except Exception:
        raise unittest.SkipTest("Ollama not reachable")
    src = "Remind me to call the dentist tomorrow at three pm and buy milk"
    out = pp.llm_clean(src, "http://127.0.0.1:11434", "llama3.2", 30)
    print("  live llm:", repr(out))
    assert out is None or len(out) > 10


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok", name)
            except unittest.SkipTest as e:
                print("skip", name, f"({e})")
