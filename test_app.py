"""Offline check of the decision logic and the HTTP API: python test_app.py"""
import copy

from fastapi.testclient import TestClient

import app
from app import DEFAULT_QUESTIONS, answer, decide, from_imajev, from_thx, omni_prompt, parse_questions

# the defaults start with every required check off: nothing can trigger a follow-up until the user ticks one
assert all(not q["expect"] for q in parse_questions(DEFAULT_QUESTIONS))

# the rest of the checks tick required on every card except dob and doc_type
cards = [dict(q, required=q["id"] not in ("dob", "doc_type")) for q in copy.deepcopy(DEFAULT_QUESTIONS)]
qs = parse_questions(cards)
assert [q["id"] for q in qs][:2] == ["signed", "dated"] and qs[0]["options"] == ["true", "false"]
assert qs[3]["expect"] == [] and qs[4]["expect"] == ["nothing missing"]  # dob is not a required check


def expect_error(items, text):
    try:
        parse_questions(items)
    except ValueError as exc:
        assert text in str(exc), exc
    else:
        raise AssertionError(f"accepted: {text}")


expect_error(cards + [{"id": "q8", "type": "noul", "question": ""}], "question is empty")
expect_error([dict(cards[4], pass_if="gone")], "'pass if' must be one of")
expect_error([dict(cards[4], options=["only one"])], "2-10 options")
expect_error([cards[0], cards[0]], "unique")


def run(threshold=0.9, **override):
    answers = {q["id"]: answer({(q["expect"] or [q["options"][0]])[0]: 0.97, "other": 0.03}) for q in qs}
    answers.update(override)
    return decide(qs, answers, threshold)


# everything as expected and confident -> approve, no letter
verdict, _, text = run()
assert verdict == "approve" and text == "", (verdict, text)

# confident "false" on signature -> follow-up letter naming the doc type; {answer} is filled in
verdict, _, text = run(signed=answer({"true": 0.02, "false": 0.98}), missing=answer({"signature": 0.95, "date": 0.05}))
assert verdict == "follow_up" and "Please sign the form" in text and "your tax form" in text, text
assert "The signature field is empty" in text, text

# below threshold -> human review wins over follow-up
verdict, rows, _ = run(signed=answer({"true": 0.02, "false": 0.98}), address=answer({"true": 0.6, "false": 0.4}))
assert verdict == "human" and dict((q["id"], s) for q, _, s in rows)["address"] == "human"

# imajev: noul folds half of unknown into p(true); abstained -> human
a = from_imajev({"type": "noul", "noul": 0.6, "unknown_probability": 0.2, "abstained": False})
assert abs(a["probs"]["true"] - 0.5) < 1e-9 and abs(a["probs"]["false"] - 0.3) < 1e-9 and a["answer"] == "true"
a = from_imajev({"type": "choice", "probabilities": {"x": 0.9, "y": 0.1}, "unknown_probability": 0.1, "abstained": True})
assert abs(a["confidence"] - 0.81) < 1e-9
assert run(signed=a)[0] == "human"

# lowering the threshold turns the same answers into an automatic decision
assert run(0.55, address=answer({"true": 0.6, "false": 0.4}))[0] == "approve"

# Jev-Omni gets noul statements as Yes/No questions, mapped back to true/false
assert omni_prompt(qs[0]) == ("Is this statement true? " + qs[0]["question"], ["Yes", "No"], {"Yes": "true", "No": "false"})

# THX-02: noul is p(yes); choice keeps its probabilities; a thought rides along
a = from_thx({"type": "noul", "noul": 0.2})
assert a["answer"] == "false" and abs(a["confidence"] - 0.8) < 1e-9 and a["thought"] is None
a = from_thx({"type": "choice", "choice": "b", "confidence": 0.5, "probabilities": {"a": 0.25, "b": 0.75}, "thought": "t"})
assert a["answer"] == "b" and a["confidence"] == 0.75 and a["thought"] == "t"

# .env fills only variables that are not already set
import os, tempfile
from pathlib import Path
env = Path(tempfile.mkdtemp()) / ".env"
env.write_text("# comment\nFDI_TEST_A='one'\nFDI_TEST_B=two\nFDI_TEST_EMPTY=\n", encoding="utf-8")
os.environ["FDI_TEST_B"] = "kept"
app.load_env(env)
assert os.environ["FDI_TEST_A"] == "one" and os.environ["FDI_TEST_B"] == "kept"
assert "FDI_TEST_EMPTY" not in os.environ  # an empty key line must not become an empty token

# HTTP API end to end, with the model calls replaced by canned answers
app.ask_imajev = lambda png, questions, calibrated: {q["id"]: answer({q["options"][-1]: 0.99, "x": 0.01}) for q in questions}
app.ask_omni = lambda png, questions: {q["id"]: answer({q["options"][0]: 0.5, "x": 0.5}) for q in questions}
app.ask_thx = lambda png, questions, think: {q["id"]: dict(answer({(q["expect"] or q["options"])[0]: 0.95, "x": 0.05}),
                                                             thought=think) for q in questions}
http = TestClient(app.api)
assert "FormTriage" in http.get("/").text
sample = http.get("/api/defaults").json()["samples"][1]
file_id = http.post(f"/api/sample/{sample}").json()["file_id"]
assert http.get("/api/preview", params={"file_id": file_id, "dpi": 72}).json()["width"] == 612
assert http.post("/api/sample/..%2Fapp.py").status_code == 404
results = http.post("/api/run", json={"file_id": file_id, "questions": cards, "models": app.MODELS,
                                      "think": "auto"}).json()["results"]
panels = http.post("/api/decide", json={"questions": cards, "results": results, "threshold": 0.9}).json()
assert panels["imajev-4b"]["verdict"] == "follow_up" and "Please sign" in panels["imajev-4b"]["letter"]
assert panels["Jev-Omni"]["verdict"] == "human"
assert panels["THX-02"]["verdict"] == "approve" and panels["THX-02"]["rows"][0]["thought"] == "auto"
assert http.post("/api/decide", json={"questions": [], "results": {}, "threshold": 0.9}).status_code == 400
print("ok")
