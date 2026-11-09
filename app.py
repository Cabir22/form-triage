"""FormTriage: drop a form, ask typed questions, compare imajev-4b, Jev-Omni and THX-02 side by side.

imajev-4b and Jev-Omni run on their authors' Hugging Face ZeroGPU Spaces (too big for local hardware);
THX-02 is HAL-X's hosted decision API. To self-host later, replace the ask_* functions; nothing else depends
on where the models run.

Run: python app.py, then open http://127.0.0.1:7860 (the page itself is index.html).
Keys go in .env next to this file (see .env.example).
"""
import base64
import os
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pymupdf
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from gradio_client import Client, handle_file
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent


def load_env(path=ROOT / ".env"):
    """KEY=value lines from .env fill in environment variables that are not already set; empty values are skipped."""
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            value = value.strip().strip("'\"")
            if sep and value and key.strip() and not key.strip().startswith("#"):
                os.environ.setdefault(key.strip(), value)


load_env()
SAMPLES = ROOT / "samples"
UPLOADS = Path(tempfile.mkdtemp(prefix="fdi-"))  # ponytail: uploads stay until the OS cleans %TEMP%
EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
# Both keys are optional; an empty value counts as unset (an empty bearer token is an illegal header).
TOKEN = os.environ.get("HF_TOKEN") or None  # a free HF token raises the ZeroGPU daily quota
HALX_KEY = os.environ.get("HALX_API_KEY") or None  # without it THX-02 runs on the free tier (60 decisions/min/IP)
HALX_URL = os.environ.get("HALX_URL") or "https://api.hal-x.ai/v1/decide"  # or a self-hosted thx02-serve
THINK = {"off": False, "auto": "auto", "on": True}
MODELS = ["imajev-4b", "Jev-Omni", "THX-02"]
OMNI_STATE = "A scanned page of a submitted form."
TYPES = ("noul", "choice")
NOUL = ["true", "false"]

SET_NAME = "Form completeness"
DEFAULT_QUESTIONS = [
    {"id": "signed", "type": "noul", "question": "The form has a handwritten signature in the signature field.",
     "required": False, "pass_if": "true", "follow_up": "Please sign the form in the signature field."},
    {"id": "dated", "type": "noul", "question": "The date next to the signature is filled in.",
     "required": False, "pass_if": "true", "follow_up": "Please write the date next to your signature."},
    {"id": "address", "type": "noul", "question": "A postal address is filled in on the form.",
     "required": False, "pass_if": "true", "follow_up": "Please provide your full mailing address."},
    {"id": "dob", "type": "noul", "question": "A date of birth is filled in on the form.",
     "required": False, "pass_if": "true", "follow_up": "Please fill in your date of birth."},
    {"id": "missing", "type": "choice", "question": "Which required field is empty?",
     "options": ["signature", "date", "address", "nothing missing"], "required": False, "pass_if": "nothing missing",
     "follow_up": "The {answer} field is empty; please complete it."},
    {"id": "doc_type", "type": "choice", "question": "What kind of document is this?",
     "options": ["tax form", "invoice", "bank statement", "letter", "other"], "required": False, "pass_if": "tax form",
     "follow_up": ""},
    {"id": "legibility", "type": "choice", "question": "How legible is the handwriting on the form?",
     "options": ["clear", "partly legible", "illegible"], "required": False, "pass_if": "clear",
     "follow_up": "Some handwriting is hard to read; please print clearly in block letters."},
]


# ---------- questions and decisions (pure, see test_app.py) ----------

def parse_questions(items):
    """Editor cards -> validated questions; raises ValueError with a message meant for the user."""
    questions = []
    for item in items or []:
        qid = str(item.get("id") or "").strip() or f"q{len(questions) + 1}"
        kind = item.get("type")
        text = str(item.get("question") or "").strip()
        if kind not in TYPES:
            raise ValueError(f"'{qid}': the type must be noul or choice.")
        if not text:
            raise ValueError(f"'{qid}': the question is empty.")
        if kind == "noul":
            options = NOUL
        else:
            options = list(dict.fromkeys(o for o in (str(o).strip() for o in item.get("options") or []) if o))
        if not 2 <= len(options) <= 10:
            raise ValueError(f"'{qid}': a choice question needs 2-10 options.")
        pass_if = str(item.get("pass_if") or "").strip()
        required = bool(item.get("required"))
        if required and pass_if not in options:
            raise ValueError(f"'{qid}': 'pass if' must be one of the question's options.")
        questions.append({"id": qid, "type": kind, "question": text, "options": options,
                          "expect": [pass_if.lower()] if required else [],
                          "follow_up": str(item.get("follow_up") or "").strip()})
    if not 1 <= len(questions) <= 8:
        raise ValueError("Use 1-8 questions (imajev's limit).")
    if len({q["id"] for q in questions}) != len(questions):
        raise ValueError("Question IDs must be unique.")
    return questions


def answer(probs, unknown=None, abstained=False):
    """Normalise one model answer; confidence is the chosen option's probability, unknown mass counted against it."""
    best = max(probs, key=probs.get)
    return {"answer": best, "confidence": probs[best], "probs": probs, "unknown": unknown, "abstained": abstained}


def from_imajev(a):
    u = a["unknown_probability"]
    if a["type"] == "noul":
        yes = a["noul"] - u / 2  # the server folds half the unknown mass into noul
        probs = {"true": yes, "false": 1 - u - yes}
    else:  # choice probabilities are over the known mass
        probs = {o: p * (1 - u) for o, p in a["probabilities"].items()}
    return answer(probs, u, a["abstained"])


def decide(questions, answers, threshold):
    """-> verdict ('approve' | 'follow_up' | 'human'), [(question, answer, status)], follow-up letter."""
    rows, lines = [], []
    for q in questions:
        a = answers.get(q["id"])
        if a is None:  # question added or renamed after the run
            rows.append((q, None, "stale"))
            continue
        if a["abstained"] or a["confidence"] < threshold:
            status = "human"
        elif q["expect"] and a["answer"].lower() not in q["expect"]:
            status = "follow"
            if q["follow_up"]:
                lines.append(q["follow_up"].replace("{answer}", a["answer"]))
        else:
            status = "auto"
        rows.append((q, a, status))
    statuses = {s for _, _, s in rows}
    verdict = "human" if statuses & {"human", "stale"} else "follow_up" if "follow" in statuses else "approve"
    doc = next((a["answer"] for q, a, s in rows if q["id"] == "doc_type" and s == "auto"), "form")
    return verdict, rows, letter(lines, doc)


def letter(lines, doc="form"):
    if not lines:
        return ""
    bullets = "\n".join(f"- {line}" for line in dict.fromkeys(lines))
    return (f"Dear applicant,\n\nThank you for submitting your {doc}. Before we can process it, we need the following:\n\n"
            f"{bullets}\n\nOnce updated, reply to this email with the corrected document.\n\n"
            "Kind regards,\nDocument Processing Team")


def report(questions, result, threshold):
    """One model's panel for the page: None (not run), an error, or verdict + rows + letter."""
    if result is None:
        return None
    if "error" in result:
        return {"error": result["error"], "quota": "ZeroGPU" in result["error"]}
    verdict, rows, text = decide(questions, result["answers"], threshold)
    return {"verdict": verdict, "seconds": result["seconds"], "letter": text,
            "rows": [{"id": q["id"], "question": q["question"], "status": s, **(a or {})} for q, a, s in rows]}


# ---------- documents and models ----------

def render(path, page, dpi):
    """PDFs and images both open as pymupdf documents and render at the chosen DPI."""
    with pymupdf.open(path) as doc:
        index = min(max(int(page or 1), 1), doc.page_count) - 1
        pix = doc[index].get_pixmap(dpi=min(max(int(dpi), 72), 300))
        return pix.tobytes("png"), pix.width, pix.height, doc.page_count


_clients = {}


def client(space):
    if space not in _clients:
        _clients[space] = Client(space, token=TOKEN, verbose=False)
    return _clients[space]


def ask_imajev(png, questions, calibrated):
    request = {
        "state": {},
        "images": ["data:image/png;base64," + base64.b64encode(png).decode()],
        "calibrated": calibrated,
        "questions": {q["id"]: {"type": "noul", "instructions": q["question"]} if q["type"] == "noul" else
                      {"type": "choice", "instructions": q["question"], "criteria": {o: None for o in q["options"]}}
                      for q in questions},
    }
    body = client("mohit67890/imajev").predict(request, api_name="/systemone")
    return {q["id"]: from_imajev(body["answers"][q["id"]]) for q in questions}


def omni_prompt(q):
    """Jev-Omni answers a question with options; a noul statement becomes a Yes/No question."""
    if q["type"] == "choice":
        return q["question"], q["options"], {}
    text = q["question"] if q["question"].endswith("?") else f"Is this statement true? {q['question']}"
    return text, ["Yes", "No"], {"Yes": "true", "No": "false"}


def ask_omni(png, questions):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
    try:
        space = client("akhilaaa3/jev-omni")
        jobs = {}
        for q in questions:
            text, options, labels = omni_prompt(q)
            jobs[q["id"]] = (labels, space.submit(OMNI_STATE, text, "\n".join(options), handle_file(f.name), "image",
                                                  api_name="/decide"))
        out = {}
        for qid, (labels, job) in jobs.items():
            label, _ = job.result()
            out[qid] = answer({labels.get(c["label"], c["label"]): c["confidence"] for c in label["confidences"]})
        return out
    finally:
        os.unlink(f.name)


def from_thx(a):
    probs = {"true": a["noul"], "false": 1 - a["noul"]} if a["type"] == "noul" else a["probabilities"]
    return dict(answer(probs), thought=a.get("thought"))


def ask_thx(png, questions, think="off"):
    """THX-02 reads the page image directly (empty state) and answers every question in one request."""
    body = {
        "model": "thx-02",
        "state": "",
        "images": ["data:image/png;base64," + base64.b64encode(png).decode()],
        "think": THINK.get(think, False),
        "return_thoughts": True,
        "questions": {q["id"]: {"type": "noul", "instructions": q["question"]} if q["type"] == "noul" else
                      {"type": "choice", "instructions": q["question"], "criteria": {o: o for o in q["options"]}}
                      for q in questions},
    }
    headers = {"Authorization": f"Bearer {HALX_KEY}"} if HALX_KEY else {}
    for attempt in range(4):  # the docs ask for back-off on 429 and 5xx
        r = httpx.post(HALX_URL, json=body, headers=headers, timeout=120)
        if r.status_code not in (429, 502, 503, 504) or attempt == 3:
            break
        time.sleep(float(r.headers.get("Retry-After") or 0.5 * 2 ** attempt))
    if r.status_code != 200:
        try:
            message = r.json()["error"]["message"]
        except Exception:
            message = r.text[:300]
        raise RuntimeError(f"HAL-X {r.status_code}: {message}")
    answers = r.json()["answers"]
    return {q["id"]: from_thx(answers[q["id"]]) for q in questions}


def timed(fn):
    start = time.perf_counter()
    try:
        return {"answers": fn(), "seconds": time.perf_counter() - start}
    except Exception as exc:  # one model failing must not hide the other's answers
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------- HTTP API for index.html ----------

api = FastAPI(title="FormTriage")
FILES = {}  # file_id -> path of an uploaded or sample document


def checked(items):
    try:
        return parse_questions(items)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def document(file_id):
    if file_id not in FILES:
        raise HTTPException(404, "The document is gone (server restarted?). Upload it again.")
    return FILES[file_id]


@api.get("/")
def index():
    return FileResponse(ROOT / "index.html")


@api.get("/api/defaults")
def defaults():
    return {"name": SET_NAME, "questions": DEFAULT_QUESTIONS, "models": MODELS, "halx_key": bool(HALX_KEY),
            "samples": sorted(p.name for p in SAMPLES.iterdir() if p.suffix.lower() in EXTENSIONS)}


@api.post("/api/upload")
def upload(file: UploadFile):
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in EXTENSIONS:
        raise HTTPException(400, "Use a PDF, PNG, JPG or WebP file.")
    file_id = uuid.uuid4().hex
    path = UPLOADS / f"{file_id}{suffix}"
    with open(path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    FILES[file_id] = path
    return {"file_id": file_id, "name": file.filename}


@api.post("/api/sample/{name}")
def sample(name: str):
    path = SAMPLES / name
    if path.parent != SAMPLES or path.suffix.lower() not in EXTENSIONS or not path.is_file():
        raise HTTPException(404, "No such sample.")
    FILES[name] = path
    return {"file_id": name, "name": name}


@api.get("/api/preview")
def preview(file_id: str, page: int = 1, dpi: int = 150):
    path = document(file_id)
    try:
        png, width, height, pages = render(path, page, dpi)
    except Exception as exc:
        raise HTTPException(400, f"Could not open the document: {exc}")
    return {"image": "data:image/png;base64," + base64.b64encode(png).decode(), "width": width, "height": height,
            "pages": pages, "kb": round(len(png) / 1024)}


class RunRequest(BaseModel):
    file_id: str
    page: int = 1
    dpi: int = 150
    questions: list[dict]
    models: list[str]
    calibrated: bool = False
    think: str = "off"


@api.post("/api/run")
def run(req: RunRequest):
    questions = checked(req.questions)
    models = [m for m in MODELS if m in req.models]
    if not models:
        raise HTTPException(400, "Select at least one model.")
    png = render(document(req.file_id), req.page, req.dpi)[0]
    calls = {"imajev-4b": lambda: ask_imajev(png, questions, req.calibrated),
             "Jev-Omni": lambda: ask_omni(png, questions),
             "THX-02": lambda: ask_thx(png, questions, req.think)}
    with ThreadPoolExecutor() as pool:
        futures = {m: pool.submit(timed, calls[m]) for m in models}
    return {"results": {m: f.result() for m, f in futures.items()}}


class DecideRequest(BaseModel):
    questions: list[dict]
    results: dict
    threshold: float


@api.post("/api/decide")
def decide_route(req: DecideRequest):
    questions = checked(req.questions)
    return {m: report(questions, req.results.get(m), req.threshold) for m in MODELS}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    print(f"FormTriage: http://127.0.0.1:{port}")
    uvicorn.run(api, host="127.0.0.1", port=port)
